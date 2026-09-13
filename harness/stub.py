"""Local stand-ins for the systems the harness talks to: a fake optimizer API and a sample sqlite database.

Run:  uv run python -m harness.stub --port 8765 --output-root stub-output --init-db local.sqlite
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, override

import duckdb

log = logging.getLogger(__name__)

SAMPLE_SQL = """
CREATE TABLE IF NOT EXISTS PORTFOLIO (PORTFOLIO_ID TEXT PRIMARY KEY, NAME TEXT, BENCHMARK TEXT, HOLDINGS_COUNT INTEGER, MARKET_CAP_BUCKET TEXT);
CREATE TABLE IF NOT EXISTS PORTFOLIO_CONSTRAINT (PORTFOLIO_ID TEXT, CONSTRAINT_TYPE TEXT, LIMIT_VALUE REAL, ENABLED INTEGER, PRIMARY KEY (PORTFOLIO_ID, CONSTRAINT_TYPE));
DELETE FROM PORTFOLIO;
DELETE FROM PORTFOLIO_CONSTRAINT;
INSERT INTO PORTFOLIO VALUES ('PF-1001','Core Equity','SP500',120,'LARGE'),('PF-1002','Mid Cap Growth','R2500',64,'MID'),('PF-1003','Small Cap Value','R2000',210,'SMALL');
INSERT INTO PORTFOLIO_CONSTRAINT VALUES ('PF-1001','SECTOR_MAX',0.25,1),('PF-1002','SECTOR_MAX',0.20,1),('PF-1002','TURNOVER_MAX',0.15,1),('PF-1003','SECTOR_MAX',0.30,0);
"""


def init_sample_db(path: Path) -> None:
    """Create the sample tables and reset their rows."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SAMPLE_SQL)
        conn.commit()
    finally:
        conn.close()


class StubOptimizer(ThreadingHTTPServer):
    """POST /runs -> {run_id}; GET /runs/{id} advances queued -> running -> done|failed; GET /ui/runs/{id} is a page."""

    def __init__(self, address: tuple[str, int], output_root: Path, polls_until_done: int = 2) -> None:
        super().__init__(address, _Handler)
        self.output_root = output_root
        self.polls_until_done = polls_until_done
        self.runs: dict[str, tuple[int, bool]] = {}
        self.lock = threading.Lock()

    @property
    def port(self) -> int:
        return int(self.server_address[1])


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        stub = cast("StubOptimizer", self.server)
        if self.path != "/runs":
            self._send(404, {"error": f"no route {self.path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        fail = isinstance(body, dict) and body.get("fail") is True
        run_id = uuid.uuid4().hex[:12]
        with stub.lock:
            stub.runs[run_id] = (0, fail)
        self._send(201, {"run_id": run_id, "status": "queued"})

    def do_GET(self) -> None:
        stub = cast("StubOptimizer", self.server)
        if self.path.startswith("/ui/runs/"):
            self._send_html(f"<h1>Run {self.path.removeprefix('/ui/runs/')}</h1><p>stub optimizer UI</p>")
            return
        run_id = self.path.removeprefix("/runs/")
        with stub.lock:
            state = stub.runs.get(run_id)
            if state is None:
                self._send(404, {"error": f"unknown run {run_id}"})
                return
            polls, fail = state[0] + 1, state[1]
            stub.runs[run_id] = (polls, fail)
        if polls < stub.polls_until_done:
            self._send(200, {"run_id": run_id, "status": "running", "output_path": None})
            return
        if fail:
            self._send(200, {"run_id": run_id, "status": "failed", "output_path": None, "error": "infeasible: constraints conflict"})
            return
        out = stub.output_root / run_id
        if not out.exists():
            write_sample_output(out, run_id)
        self._send(200, {"run_id": run_id, "status": "done", "output_path": str(out)})

    def _send(self, code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)


def write_sample_output(out: Path, run_id: str) -> None:
    """Two parquet files shaped like a plausible optimizer result: holdings and a one-row summary."""
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    holdings = str(out / "holdings.parquet").replace("'", "''")
    summary = str(out / "summary.parquet").replace("'", "''")
    con.execute(
        "COPY (SELECT 'PF-1002' AS portfolio_id, 'SEC' || lpad(i::VARCHAR, 3, '0') AS security_id,"
        " ['TECH','FIN','HLTH','ENGY','UTIL'][1 + i % 5] AS sector, round(1.0 / 40 + (i % 7 - 3) * 0.002, 6) AS weight"
        f" FROM range(1, 41) t(i)) TO '{holdings}' (FORMAT PARQUET)"
    )
    con.execute(f"COPY (SELECT '{run_id}' AS run_id, 0.0123 AS objective_value, 0.0187 AS tracking_error, 0.12 AS turnover, 40 AS holdings) TO '{summary}' (FORMAT PARQUET)")
    con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-root", type=Path, default=Path("stub-output"), help="where fake parquet output is written")
    parser.add_argument("--init-db", type=Path, help="also (re)create the sample sqlite database at this path")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.init_db is not None:
        init_sample_db(args.init_db)
        log.info("sample database ready", extra={"path": str(args.init_db)})
    server = StubOptimizer((args.host, args.port), args.output_root)
    log.info("stub optimizer listening", extra={"host": args.host, "port": server.port})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
