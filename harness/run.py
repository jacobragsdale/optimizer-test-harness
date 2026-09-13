"""One test case lifecycle: plan, apply, submit, poll, revert.

`run_case` reverts inside a `finally` block. Neither the model driving it nor a failure in the optimizer call can skip
the revert. The only way changes survive is a failed revert, which leaves the PENDING_REVERT marker behind so every later
command shouts about it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from harness import HarnessError
from harness.config import RECORD, STRICT, Config, Settings
from harness.db import Connection, LedgerEntry, apply, check_qa_target, connect, describe_target, jsonable, mark_revert_failed, read_one, revert
from harness.optimizer import Http, RunRecord, poll, submit, urllib_http, utc_now
from harness.spec import Spec, check_changes

log = logging.getLogger(__name__)

MARKER = "PENDING_REVERT"
Progress = Callable[[str], None]
Connector = Callable[[], Connection]


class Verdict(BaseModel):
    model_config = STRICT
    result: Literal["pass", "fail", "inconclusive"]
    evidence: list[str]
    notes: str
    recorded_at: datetime


class Manifest(BaseModel):
    """The audit record for one test case run. One JSON file per run under runs/<case_id>/."""

    model_config = RECORD
    case_id: str
    started_at: datetime
    finished_at: datetime | None = None
    spec: Spec
    dialect: str
    db_target: str
    ledger: list[LedgerEntry] = Field(default_factory=list)
    run: RunRecord | None = None
    verdict: Verdict | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def all_reverted(self) -> bool:
        return all(e.reverted and e.revert_ok for e in self.ledger)


def save(manifest: Manifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def load_manifest(path: Path) -> Manifest:
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def pending_revert(runs_dir: Path) -> Path | None:
    """The manifest whose changes are still in QA, or None."""
    marker = runs_dir / MARKER
    return Path(marker.read_text(encoding="utf-8").strip()) if marker.exists() else None


def plan_text(spec: Spec, config: Config, conn: Connection) -> str:
    """What run_case would do, with current database values. Read-only."""
    lines = [
        f"CASE {spec.case_id}: {spec.title}",
        f"interpretation: {spec.interpretation.strip()}",
        f"portfolios: {', '.join(spec.portfolio.ids) or '(none)'}  [{spec.portfolio.rationale.strip()}]",
        "",
        f"data changes ({len(spec.changes)}):",
    ]
    for change in spec.changes:
        current = jsonable(read_one(conn, config.db.dialect, change.table, change.key, change.column))
        lines.append(f"  {change.table} {change.key} . {change.column}: {current!r} -> {change.value!r}")
    lines += ["", "optimizer request:", json.dumps(spec.run.params, indent=2), "", f"expected outcome: {spec.expected.outcome}", "evaluation plan:"]
    lines += [f"  - {step}" for step in spec.expected.evaluation_plan]
    return "\n".join(lines)


def _connector(config: Config, settings: Settings, connect_db: Connector | None) -> Connector:
    check_qa_target(settings.db_dsn, config.db.qa_markers)
    return (lambda: connect(config.db.dialect, settings.db_dsn)) if connect_db is None else connect_db


def run_case(
    spec: Spec,
    config: Config,
    settings: Settings,
    runs_dir: Path,
    *,
    connect_db: Connector | None = None,
    http: Http = urllib_http,
    now: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
    progress: Progress = log.info,
) -> tuple[Manifest, Path]:
    """Apply the spec's changes, run the optimizer, revert. Always returns a saved manifest; errors are recorded in it."""
    problems = check_changes(spec, config)
    if problems:
        msg = "spec is not runnable:\n  " + "\n  ".join(problems)
        raise HarnessError(msg)
    pending = pending_revert(runs_dir)
    if pending is not None:
        msg = f"unreverted changes from {pending}; run `harness revert {pending} --yes` first"
        raise HarnessError(msg)
    connector = _connector(config, settings, connect_db)

    started = now()
    manifest = Manifest(case_id=spec.case_id, started_at=started, spec=spec, dialect=config.db.dialect, db_target=describe_target(settings.db_dsn))
    path = (runs_dir / spec.case_id / f"{started:%Y%m%dT%H%M%SZ}.json").resolve()
    save(manifest, path)
    conn = connector()
    marker = runs_dir / MARKER
    marker.write_text(str(path), encoding="utf-8")

    def checkpoint(record: RunRecord) -> None:
        manifest.run = record
        save(manifest, path)
        progress(f"status: {record.status_history[-1].status}")

    try:
        manifest.ledger = apply(conn, config.db.dialect, spec.changes)
        save(manifest, path)
        progress(f"applied {len(manifest.ledger)} change(s)")
        record = submit(config.api, settings.api_token, spec.run.params, http=http, now=now)
        manifest.run = record
        save(manifest, path)
        progress(f"submitted run {record.run_id}  {record.url}")
        poll(config.api, settings.api_token, record, http=http, now=now, sleep=sleep, on_event=checkpoint)
        manifest.run = record
        progress(f"run finished: {record.final_status}  output: {record.output_path}")
    except Exception as exc:  # deliberate: the manifest must record the failure and the revert below must still run
        manifest.errors.append(f"{type(exc).__name__}: {exc}")
        log.exception("case failed", extra={"case_id": spec.case_id})
    finally:
        try:
            manifest.ledger = revert(conn, config.db.dialect, manifest.ledger)
        except Exception as exc:  # deliberate: record, keep the marker, keep going
            manifest.errors.append(f"REVERT FAILED {type(exc).__name__}: {exc}")
            manifest.ledger = mark_revert_failed(manifest.ledger, str(exc))
            log.exception("revert failed", extra={"case_id": spec.case_id})
        finally:
            conn.close()
        if manifest.all_reverted:
            marker.unlink(missing_ok=True)
            progress(f"reverted {len(manifest.ledger)} change(s); QA is back to how it was")
        else:
            progress(f"REVERT INCOMPLETE: changes remain in QA; marker left at {marker}")
        manifest.finished_at = now()
        save(manifest, path)
    return manifest, path


def revert_manifest(path: Path, config: Config, settings: Settings, runs_dir: Path, *, connect_db: Connector | None = None) -> Manifest:
    """Manually revert whatever a manifest's ledger still holds. Clears the marker if it points at this manifest."""
    manifest = load_manifest(path)
    conn = _connector(config, settings, connect_db)()
    failure: Exception | None = None
    try:
        manifest.ledger = revert(conn, config.db.dialect, manifest.ledger)
    except Exception as exc:  # deliberate: record before re-raising
        manifest.ledger = mark_revert_failed(manifest.ledger, str(exc))
        manifest.errors.append(f"REVERT FAILED {type(exc).__name__}: {exc}")
        log.exception("manual revert failed", extra={"case_id": manifest.case_id, "manifest": str(path)})
        failure = exc
    finally:
        conn.close()
        save(manifest, path)
    if failure is not None:
        msg = f"revert failed; changes are still in QA: {failure}"
        raise HarnessError(msg) from failure
    if pending_revert(runs_dir) == path.resolve():
        (runs_dir / MARKER).unlink()
    return manifest


def record_verdict(path: Path, result: Literal["pass", "fail", "inconclusive"], evidence: list[str], notes: str, now: Callable[[], datetime] = utc_now) -> Manifest:
    manifest = load_manifest(path)
    manifest.verdict = Verdict(result=result, evidence=evidence, notes=notes, recorded_at=now())
    save(manifest, path)
    return manifest
