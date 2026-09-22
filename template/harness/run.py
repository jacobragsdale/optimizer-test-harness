"""One test case lifecycle: login, setup, run, collect, revert.

`run_case` reverts inside a `finally` block. Neither the model driving it nor a failure in the app can skip the
revert. The only ways changes survive are a failed revert or a killed process. Both leave the PENDING_REVERT marker
behind so every later command shouts about it, and every undo record is on disk before the change it undoes.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, JsonValue

from harness import HarnessError
from harness.api import Http, StepRecord, call, render, urllib_session, utc_now
from harness.config import RECORD, STRICT, Config, Operation, Settings, variables
from harness.db import Connection, LedgerEntry, apply, bind_vars, check_qa_target, connect, describe_target, jsonable, mark_revert_failed, plain, query, revert, select_row
from harness.spec import ApiStep, DbInsert, DbStep, DbUpdate, Spec, check_spec, db_key

log = logging.getLogger(__name__)

MARKER = "PENDING_REVERT"
COLLECT_LIMIT = 100_000
Progress = Callable[[str], None]
Connector = Callable[[], Connection]


class Verdict(BaseModel):
    model_config = STRICT
    result: Literal["pass", "fail", "inconclusive"]
    evidence: list[str]
    notes: str
    recorded_at: datetime


class Manifest(BaseModel):
    """The audit record for one test case run: runs/<case_id>/<timestamp>/manifest.json, next to the saved responses."""

    model_config = RECORD
    case_id: str
    started_at: datetime
    finished_at: datetime | None = None
    spec: Spec
    dialect: str
    db_target: str
    api_target: str
    ledger: list[LedgerEntry] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    vars: dict[str, JsonValue] = Field(default_factory=dict)
    collected: list[str] = Field(default_factory=list)
    link: str | None = None
    verdict: Verdict | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def all_reverted(self) -> bool:
        return all(e.done for e in self.ledger)

    @property
    def run_ok(self) -> bool | None:
        """True when every run step succeeded, False when the app said no, None when the run never finished."""
        runs = [s for s in self.steps if s.phase == "run"]
        if any(s.ok is False for s in runs):
            return False
        return True if len(runs) == len(self.spec.run) and all(s.ok for s in runs) else None

    @property
    def outcome(self) -> str:
        """What BAs see: the rejecting step's status, else the last polled status (the job's), else the last HTTP status."""
        runs = [s for s in self.steps if s.phase == "run"]
        decisive = next((s for s in runs if s.ok is False), None) or next((s for s in reversed(runs) if s.final_status), None) or (runs[-1] if runs else None)
        return decisive.outcome if decisive else "-"


def save(manifest: Manifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def manifest_path(target: Path) -> Path:
    """Accept a run folder or its manifest.json."""
    return target / "manifest.json" if target.is_dir() else target


def load_manifest(path: Path) -> Manifest:
    return Manifest.model_validate_json(manifest_path(path).read_text(encoding="utf-8"))


def pending_revert(runs_dir: Path) -> Path | None:
    """The manifest whose changes are still in QA, or None."""
    marker = runs_dir / MARKER
    return Path(marker.read_text(encoding="utf-8").strip()) if marker.exists() else None


def plan_text(spec: Spec, config: Config, conn: Connection) -> str:
    """What run_case would do, with current database values. Read-only."""
    lines = [
        f"CASE {spec.case_id}: {spec.title}",
        f"interpretation: {spec.interpretation.strip()}",
        f"subject: {', '.join(spec.subject.ids) or '(none)'}  [{spec.subject.rationale.strip()}]",
        "",
        f"setup ({len(spec.setup)}):",
    ]
    if config.api.login:
        lines.insert(4, f"login: {_api_line(ApiStep(api=config.api.login), config)}")
    lines += [f"  {_api_line(s, config) if isinstance(s, ApiStep) else _db_line(s, config, conn)}" for s in spec.setup]
    lines += ["", f"run ({len(spec.run)}):", *(f"  {_api_line(s, config)}" for s in spec.run)]
    lines += ["", f"collect before revert ({len(spec.collect)}):", *(f"  {c.name}: {c.sql.strip()}" for c in spec.collect)]
    lines += ["", f"expected outcome: {spec.expected.outcome}", "evaluation plan:", *(f"  - {step}" for step in spec.expected.evaluation_plan)]
    return "\n".join(lines)


def _api_line(step: ApiStep, config: Config) -> str:
    op = _op(config, step.api)
    body = op.body if step.body is None else step.body
    line = f"api {op.name}: {op.method} {config.api.base_url.rstrip('/')}{op.path}" + (f" body {json.dumps(body)}" if body is not None else "")
    if op.poll:
        line += f"  (polls {op.poll.field} until {'|'.join(op.poll.until)})"
    return line + (f"  (undo: {op.undo})" if op.undo else "")


def _db_line(step: DbStep, config: Config, conn: Connection) -> str:
    allowed = config.allowed_table(step.table)
    if allowed is None:
        return f"db {step.db} {step.table}: NOT ALLOWED"
    key = db_key(step, allowed)
    head = f"db {step.db} {step.table} {key}"
    if isinstance(step, DbInsert):
        exists = select_row(conn, config.db.dialect, step.table, key, list(key)) is not None
        return f"{head}: ROW EXISTS, run-case will refuse" if exists else f"{head}: row absent; insert {step.values}"
    row = select_row(conn, config.db.dialect, step.table, key, list(step.set) if isinstance(step, DbUpdate) else None)
    if row is None:
        return f"{head}: NO SUCH ROW, run-case will refuse"
    if isinstance(step, DbUpdate):
        return f"{head}: " + ", ".join(f"{c} {plain(jsonable(row[c]))!r} -> {v!r}" for c, v in step.set.items())
    return f"{head}: deletes { ({c: plain(jsonable(v)) for c, v in row.items()}) }"


def _op(config: Config, name: str) -> Operation:
    op = config.op(name)
    if op is None:  # check_spec already refused this
        msg = f"operation {name!r} is not defined in harness.toml"
        raise HarnessError(msg)
    return op


def _connector(config: Config, settings: Settings, connect_db: Connector | None) -> Connector:
    check_qa_target(settings.db_dsn, config.db.qa_markers)
    check_qa_target(config.api.base_url, config.db.qa_markers, "api.base_url")
    return (lambda: connect(config.db.dialect, settings.db_dsn)) if connect_db is None else connect_db


@dataclass
class _Case:
    """The state one case accumulates. Every change is saved to the manifest as it happens."""

    manifest: Manifest
    path: Path
    config: Config
    http: Http
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    progress: Progress

    @property
    def variables(self) -> dict[str, JsonValue]:
        return {"base_url": self.config.api.base_url} | self.manifest.vars

    def save(self) -> None:
        save(self.manifest, self.path)

    def call(self, op: Operation, body: JsonValue, phase: Literal["login", "setup", "run", "undo"], values: dict[str, JsonValue] | None = None) -> StepRecord:
        n = len(self.manifest.steps)

        def checkpoint(record: StepRecord) -> None:
            self.manifest.steps = [*self.manifest.steps[:n], record]
            self.save()
            self.progress(f"{record.op}: {record.status_history[-1].status}")

        record = call(self.config.api, op, body, self.variables if values is None else values, self.http, self.path.parent, phase, now=self.now, sleep=self.sleep, on_event=checkpoint)
        self.manifest.steps = [*self.manifest.steps[:n], record]
        if phase != "undo":
            self.manifest.vars = self.manifest.vars | record.captured
        self.save()
        return record

    def login(self) -> None:
        if self.config.api.login:
            record = self.call(_op(self.config, self.config.api.login), None, "login")
            if not record.ok:
                msg = f"login failed: {record.detail}"
                raise HarnessError(msg)

    def setup(self, conn: Connection) -> None:
        """Consecutive DB steps commit together; each API step is its own unit."""
        group: list[DbStep] = []
        for step in self.manifest.spec.setup:
            if not isinstance(step, ApiStep):
                group.append(step)
                continue
            if group:
                self._db_setup(conn, group)
                group = []
            self._api_setup(step)
        if group:
            self._db_setup(conn, group)

    def _db_setup(self, conn: Connection, steps: Sequence[DbStep]) -> None:
        base = list(self.manifest.ledger)

        def staged(entries: list[LedgerEntry]) -> None:
            self.manifest.ledger = base + entries
            self.save()

        apply(conn, self.config, steps, on_staged=staged)
        self.progress(f"applied {len(steps)} database change(s)")

    def _api_setup(self, step: ApiStep) -> None:
        op = _op(self.config, step.api)
        i = len(self.manifest.ledger)
        intent = LedgerEntry(kind="api", op=op.name, undo=op.undo or "", request=op.body if step.body is None else step.body)
        self.manifest.ledger = [*self.manifest.ledger, intent]
        self.save()  # the intent is on disk before the call, so a crash mid-call is visible
        record = self.call(op, step.body, "setup")
        self.manifest.ledger = [*self.manifest.ledger[:i], intent.model_copy(update={"http_status": record.http_status, "vars": record.captured})]
        self.save()
        if not record.ok:
            msg = f"setup step {op.name} did not succeed: {record.detail}"
            raise HarnessError(msg)
        self.progress(f"setup {op.name}: {record.outcome} {record.captured or ''}")

    def run(self) -> None:
        for step in self.manifest.spec.run:
            record = self.call(_op(self.config, step.api), step.body, "run")
            self.progress(f"{record.op}: {record.outcome}")
            if not record.ok:
                self.progress(f"stopping the run: {record.op} did not succeed ({record.detail})")
                return

    def collect(self, conn: Connection) -> None:
        for c in self.manifest.spec.collect:
            if missing := variables(c.sql) - set(self.variables):
                self.progress(f"collect {c.name}: skipped, {sorted(missing)} never captured (the run stopped early)")
                continue
            try:
                sql, params = bind_vars(self.config.db.dialect, c.sql, self.variables)
                columns, rows = query(conn, sql, COLLECT_LIMIT, params)
            except Exception as exc:  # deliberate: one bad query must not stop the others or the revert
                self.manifest.errors = [*self.manifest.errors, f"collect {c.name}: {type(exc).__name__}: {exc}"]
                log.exception("collect failed", extra={"collect": c.name})
                continue
            with (self.path.parent / f"{c.name}.csv").open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                writer.writerows([plain(jsonable(v)) for v in row] for row in rows)
            self.manifest.collected = [*self.manifest.collected, f"{c.name}.csv"]
            self.progress(f"collected {c.name}: {len(rows)} row(s)")
        self.save()

    def undo(self, op: Operation, captured: dict[str, JsonValue]) -> StepRecord:
        return self.call(op, None, "undo", {"base_url": self.config.api.base_url} | captured)


def run_case(
    spec: Spec,
    config: Config,
    settings: Settings,
    runs_dir: Path,
    *,
    connect_db: Connector | None = None,
    http: Http | None = None,
    now: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
    progress: Progress = log.info,
) -> tuple[Manifest, Path]:
    """Set up, run, collect, revert. Always returns a saved manifest; errors are recorded in it."""
    problems = check_spec(spec, config)
    if problems:
        msg = "spec is not runnable:\n  " + "\n  ".join(problems)
        raise HarnessError(msg)
    connector = _connector(config, settings, connect_db)

    started = now()
    path = (runs_dir / spec.case_id / f"{started:%Y%m%dT%H%M%SZ}" / "manifest.json").resolve()
    manifest = Manifest(case_id=spec.case_id, started_at=started, spec=spec, dialect=config.db.dialect, db_target=describe_target(settings.db_dsn), api_target=config.api.base_url)
    marker = runs_dir / MARKER
    _claim(marker, path)
    case = _Case(manifest, path, config, http or urllib_session(config.api.timeout_seconds), now, sleep, progress)
    case.save()
    conn: Connection | None = None
    try:
        conn = connector()
        case.login()
        case.setup(conn)
        case.run()
        case.collect(conn)
    except Exception as exc:  # deliberate: the manifest must record the failure and the revert below must still run
        manifest.errors = [*manifest.errors, f"{type(exc).__name__}: {exc}"]
        log.exception("case failed", extra={"case_id": spec.case_id})
    finally:
        try:
            manifest.ledger, errors = revert_ledger(manifest.ledger, config, conn, case.undo)
            manifest.errors = [*manifest.errors, *errors]
        finally:
            if conn is not None:
                conn.close()
        if manifest.all_reverted:
            marker.unlink(missing_ok=True)
            progress(f"reverted {len(manifest.ledger)} setup step(s); QA is back to how it was")
        else:
            progress(f"REVERT INCOMPLETE: changes remain in QA; marker left at {marker}")
        manifest.link = report_link(config, case.variables)
        manifest.finished_at = now()
        case.save()
    return manifest, path


def _segments(entries: Sequence[LedgerEntry]) -> list[list[int]]:
    """Ledger indices grouped so consecutive DB entries revert in one transaction and each API entry on its own."""
    segments: list[list[int]] = []
    for i, e in enumerate(entries):
        if e.kind == "db" and segments and entries[segments[-1][-1]].kind == "db":
            segments[-1].append(i)
        else:
            segments.append([i])
    return segments


def revert_ledger(ledger: Sequence[LedgerEntry], config: Config, conn: Connection | None, undo: Callable[[Operation, dict[str, JsonValue]], StepRecord]) -> tuple[list[LedgerEntry], list[str]]:
    """Undo everything in reverse order. A failed segment is marked and recorded; the others still revert."""
    entries = list(ledger)
    errors: list[str] = []
    for segment in reversed(_segments(entries)):
        todo = [entries[i] for i in segment]
        if all(e.done for e in todo):
            continue
        if todo[0].kind == "api":
            entries[segment[0]] = _undo_api(todo[0], config, undo, errors)
            continue
        try:
            if conn is None:
                msg = "no database connection"
                raise HarnessError(msg)
            done = revert(conn, config.db.dialect, todo)
        except Exception as exc:  # deliberate: record, keep the marker, keep reverting the rest
            errors.append(f"REVERT FAILED {type(exc).__name__}: {exc}")
            done = mark_revert_failed(todo, str(exc))
            log.exception("revert failed")
        for i, e in zip(segment, done, strict=True):
            entries[i] = e
    return entries, errors


def _undo_api(e: LedgerEntry, config: Config, undo: Callable[[Operation, dict[str, JsonValue]], StepRecord], errors: list[str]) -> LedgerEntry:
    if e.http_status is None:
        note = "the call was interrupted, so whether it took effect is unknown: check the app, clean up by hand, then delete runs/PENDING_REVERT"
        errors.append(f"REVERT FAILED {e.op}: {note}")
        return e.model_copy(update={"revert_ok": False, "note": note})
    if not 200 <= e.http_status < 300:
        return e.model_copy(update={"reverted": True, "revert_ok": True, "note": "the call failed, so there was nothing to undo"})
    try:
        record = undo(_op(config, e.undo), e.vars)
    except Exception as exc:  # deliberate: record, keep the marker, keep reverting the rest
        errors.append(f"REVERT FAILED {e.op} via {e.undo}: {type(exc).__name__}: {exc}")
        log.exception("undo failed", extra={"op": e.op})
        return e.model_copy(update={"reverted": False, "revert_ok": False, "note": str(exc)})
    if record.ok or record.http_status == 404:
        note = "" if record.ok else "undo returned 404: already gone"
        return e.model_copy(update={"reverted": True, "revert_ok": True, "note": note})
    errors.append(f"REVERT FAILED {e.op} via {e.undo}: {record.detail}")
    return e.model_copy(update={"reverted": False, "revert_ok": False, "note": record.detail})


def report_link(config: Config, values: dict[str, JsonValue]) -> str | None:
    if config.report.link is None:
        return None
    try:
        return str(render(config.report.link, values))
    except HarnessError:
        return None


def output_paths(manifest: Manifest, config: Config) -> list[Path]:
    """The run folder, plus each [outputs] path whose variables this run captured."""
    values = {"base_url": config.api.base_url} | manifest.vars
    return [Path(str(render(t, values))) for t in config.outputs.paths if variables(t) <= set(values)]


def _claim(marker: Path, path: Path) -> None:
    """Create the marker atomically. It exists while a run is in flight or its changes are unreverted, so it is also the run lock."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as f:
            f.write(str(path))
    except FileExistsError:
        pending = pending_revert(marker.parent)
        msg = f"{marker} points at {pending}: another run is in progress, or its changes were never reverted. If no run is in progress: harness revert {pending} --yes"
        raise HarnessError(msg) from None


def clear_marker(runs_dir: Path, path: Path) -> bool:
    """Remove the marker if it points at this manifest. True if it did."""
    if pending_revert(runs_dir) != manifest_path(path).resolve():
        return False
    (runs_dir / MARKER).unlink()
    return True


def revert_manifest(
    path: Path, config: Config, settings: Settings, runs_dir: Path, *, connect_db: Connector | None = None, http: Http | None = None, now: Callable[[], datetime] = utc_now
) -> Manifest:
    """Manually revert whatever a manifest's ledger still holds. Clears the marker if it points at this manifest."""
    path = manifest_path(path)
    manifest = load_manifest(path)
    todo = [e for e in manifest.ledger if not e.done]
    connector = _connector(config, settings, connect_db)
    case = _Case(manifest, path, config, http or urllib_session(config.api.timeout_seconds), now, time.sleep, log.info)
    conn = connector() if any(e.kind == "db" for e in todo) else None
    errors: list[str] = []
    try:
        if any(e.kind == "api" and e.http_status is not None and 200 <= e.http_status < 300 for e in todo):
            case.login()
        manifest.ledger, errors = revert_ledger(manifest.ledger, config, conn, case.undo)
        manifest.errors = [*manifest.errors, *errors]
    finally:
        if conn is not None:
            conn.close()
        save(manifest, path)
    if errors:
        msg = "revert failed; changes are still in QA:\n  " + "\n  ".join(errors)
        raise HarnessError(msg)
    clear_marker(runs_dir, path)
    return manifest


def record_verdict(path: Path, result: Literal["pass", "fail", "inconclusive"], evidence: list[str], notes: str, now: Callable[[], datetime] = utc_now) -> Manifest:
    path = manifest_path(path)
    manifest = load_manifest(path)
    manifest.verdict = Verdict(result=result, evidence=evidence, notes=notes, recorded_at=now())
    save(manifest, path)
    return manifest
