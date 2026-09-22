"""A demo web app for local dry runs and the harness's own tests: a small pricing service over a sample sqlite database.

It exercises every harness feature. Login sets a session cookie. Price lists are created and deleted over the API
(setup with an undo). Jobs are submitted, then polled to done or failed. On done the app writes rows of its own
(JOB_RESULT), serves prices as JSON, and exports a CSV file to an output folder. Discounts are read from the
database when a job is submitted, so a spec's DB setup changes what the job produces.

Run:  uv run python -m harness.stub --port 8765 --output-root demo-output --init-db demo.sqlite
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, override

log = logging.getLogger(__name__)

PASSWORD = "demo"  # the demo app's fixed password; .env.example sets HARNESS_APP_PASSWORD to it
SAMPLE_SQL = """
DROP TABLE IF EXISTS PRODUCT;
DROP TABLE IF EXISTS DISCOUNT_RULE;
DROP TABLE IF EXISTS JOB_RESULT;
CREATE TABLE PRODUCT (PRODUCT_ID TEXT PRIMARY KEY, NAME TEXT, CATEGORY TEXT, BASE_PRICE REAL);
CREATE TABLE DISCOUNT_RULE (RULE_ID TEXT PRIMARY KEY, PRODUCT_ID TEXT, PERCENT REAL, ENABLED INTEGER, CREATED_ON DATE);
CREATE TABLE JOB_RESULT (JOB_ID TEXT, PRODUCT_ID TEXT, FINAL_PRICE REAL);
INSERT INTO PRODUCT VALUES ('P-100','Desk Lamp','LIGHTING',40.0),('P-200','Office Chair','SEATING',250.0),('P-300','Standing Desk','DESKS',600.0),('P-400','Monitor Arm','ACCESSORIES',90.0);
INSERT INTO DISCOUNT_RULE VALUES ('R-1','P-100',10,1,'2026-01-15'),('R-2','P-200',15,1,'2026-02-01'),('R-3','P-300',5,0,'2026-03-10');
"""


def init_sample_db(path: Path) -> None:
    """Create the sample tables with their original rows, replacing whatever was there."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SAMPLE_SQL)
        conn.commit()
    finally:
        conn.close()


def price(db: Path, product_ids: list[str], markup_percent: float) -> list[dict[str, object]]:
    """Final price per product: base price, plus the price list's markup, minus the sum of enabled discounts.

    Raises ValueError when a product's discounts add up to more than 100%.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        base = dict(conn.execute(f"SELECT PRODUCT_ID, BASE_PRICE FROM PRODUCT WHERE PRODUCT_ID IN ({','.join('?' * len(product_ids))})", product_ids).fetchall())
        discount = dict(conn.execute("SELECT PRODUCT_ID, SUM(PERCENT) FROM DISCOUNT_RULE WHERE ENABLED = 1 GROUP BY PRODUCT_ID").fetchall())
    finally:
        conn.close()
    rows: list[dict[str, object]] = []
    for pid in product_ids:
        percent = float(discount.get(pid, 0.0))
        if percent > 100:
            msg = f"invalid discount: {pid} totals {percent:g}%, over 100%"
            raise ValueError(msg)
        final = round(float(base[pid]) * (1 + markup_percent / 100) * (1 - percent / 100), 2)
        rows.append({"product_id": pid, "base_price": base[pid], "markup_percent": markup_percent, "discount_percent": percent, "final_price": final})
    return rows


@dataclass
class _Job:
    prices: list[dict[str, object]] | None
    error: str | None
    polls: int = 0
    finished: bool = False


class DemoApp(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], db: Path, output_root: Path, polls_until_done: int = 2) -> None:
        super().__init__(address, _Handler)
        self.db = db
        self.output_root = output_root
        self.polls_until_done = polls_until_done
        self.sessions: set[str] = set()
        self.price_lists: dict[str, float] = {}
        self.jobs: dict[str, _Job] = {}
        self.lock = threading.Lock()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def finish(self, job_id: str, job: _Job) -> Path:
        """Write the app's own outputs once: JOB_RESULT rows and a CSV export."""
        out = self.output_root / job_id
        if job.finished or job.prices is None:
            return out
        conn = sqlite3.connect(self.db)
        try:
            conn.executemany("INSERT INTO JOB_RESULT VALUES (?, ?, ?)", [(job_id, p["product_id"], p["final_price"]) for p in job.prices])
            conn.commit()
        finally:
            conn.close()
        out.mkdir(parents=True, exist_ok=True)
        with (out / "prices_export.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(job.prices[0]))
            writer.writeheader()
            writer.writerows(job.prices)
        job.finished = True
        return out


_JOB = re.compile(r"^/jobs/([\w-]+)(/prices)?$")
_PRICE_LIST = re.compile(r"^/price-lists/([\w-]+)$")


class _Handler(BaseHTTPRequestHandler):
    @property
    def app(self) -> DemoApp:
        return cast("DemoApp", self.server)

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/session":
            self._login(body)
        elif not self._authed():
            self._send(401, {"error": "log in first"})
        elif self.path == "/price-lists":
            self._create_price_list(body)
        elif self.path == "/jobs":
            self._submit(body)
        else:
            self._send(404, {"error": f"no route {self.path}"})

    def do_DELETE(self) -> None:
        match = _PRICE_LIST.match(self.path)
        if not self._authed():
            self._send(401, {"error": "log in first"})
        elif match is None:
            self._send(404, {"error": f"no route {self.path}"})
        elif self.app.price_lists.pop(match.group(1), None) is None:
            self._send(404, {"error": f"no price list {match.group(1)}"})
        else:
            self._send(200, {"deleted": match.group(1)})

    def do_GET(self) -> None:
        if self.path.startswith("/ui/jobs/"):
            self._send_raw(200, "text/html", f"<h1>Job {self.path.removeprefix('/ui/jobs/')}</h1><p>demo pricing app</p>".encode())
            return
        match = _JOB.match(self.path)
        if not self._authed():
            self._send(401, {"error": "log in first"})
            return
        job = self.app.jobs.get(match.group(1)) if match else None
        if match is None or job is None:
            self._send(404, {"error": f"no route or job {self.path}"})
        elif match.group(2):
            self._prices(job)
        else:
            self._status(match.group(1), job)

    def _login(self, body: dict[str, object]) -> None:
        if body.get("password") != PASSWORD:
            self._send(401, {"error": "bad credentials"})
            return
        token = uuid.uuid4().hex
        self.app.sessions.add(token)
        self._send(200, {"user": body.get("user")}, {"Set-Cookie": f"session={token}; Path=/"})

    def _create_price_list(self, body: dict[str, object]) -> None:
        markup = body.get("markup_percent")
        if not isinstance(markup, int | float):
            self._send(422, {"error": "markup_percent must be a number"})
            return
        list_id = f"PL-{uuid.uuid4().hex[:6]}"
        self.app.price_lists[list_id] = float(markup)
        self._send(201, {"id": list_id, "name": body.get("name"), "markup_percent": markup})

    def _submit(self, body: dict[str, object]) -> None:
        product_ids = body.get("product_ids")
        list_id = body.get("price_list_id")
        if not isinstance(product_ids, list) or not product_ids:
            self._send(422, {"error": "product_ids must be a non-empty list"})
            return
        if list_id is not None and list_id not in self.app.price_lists:
            self._send(404, {"error": f"no price list {list_id}"})
            return
        markup = self.app.price_lists.get(str(list_id), 0.0) if list_id is not None else 0.0
        try:
            job = _Job(price(self.app.db, [str(p) for p in product_ids], markup), None)
        except KeyError as exc:
            self._send(422, {"error": f"unknown product {exc}"})
            return
        except ValueError as exc:
            job = _Job(None, str(exc))
        job_id = uuid.uuid4().hex[:12]
        self.app.jobs[job_id] = job
        self._send(201, {"job_id": job_id, "status": "queued"})

    def _status(self, job_id: str, job: _Job) -> None:
        with self.app.lock:
            job.polls += 1
            if job.polls < self.app.polls_until_done:
                self._send(200, {"job_id": job_id, "status": "running"})
            elif job.prices is None:
                self._send(200, {"job_id": job_id, "status": "failed", "error": job.error})
            else:
                out = self.app.finish(job_id, job)
                self._send(200, {"job_id": job_id, "status": "done", "output_path": str(out)})

    def _prices(self, job: _Job) -> None:
        if not job.finished:
            self._send(409, {"error": "job has not finished"})
        else:
            self._send(200, {"prices": job.prices})

    def _authed(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        return "session" in cookie and cookie["session"].value in self.app.sessions

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        return {str(k): v for k, v in body.items()} if isinstance(body, dict) else {}

    def _send(self, code: int, payload: dict[str, object], headers: dict[str, str] | None = None) -> None:
        self._send_raw(code, "application/json", json.dumps(payload).encode(), headers)

    def _send_raw(self, code: int, content_type: str, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-root", type=Path, default=Path("demo-output"), help="where job exports are written")
    parser.add_argument("--init-db", type=Path, required=True, help="the sample sqlite database; recreated with its original rows at startup")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_sample_db(args.init_db)
    server = DemoApp((args.host, args.port), args.init_db, args.output_root)
    log.info("demo app listening", extra={"host": args.host, "port": server.port, "db": str(args.init_db)})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
