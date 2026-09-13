"""Database adapter: read-only queries, allowlisted single-row updates with a before-image ledger, and revert.

Identifiers are interpolated into SQL only after `harness.spec.check_changes` matched them against the allowlist.
Values always travel as bind parameters.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, cast

from pydantic import BaseModel, JsonValue

from harness import HarnessError
from harness.config import STRICT, Dialect
from harness.spec import Change, Scalar

log = logging.getLogger(__name__)


class Cursor(Protocol):
    """The DB-API 2.0 subset the harness uses."""

    @property
    def rowcount(self) -> int: ...
    @property
    def description(self) -> Sequence[Sequence[object]] | None: ...
    def execute(self, sql: str, params: Sequence[Scalar] = (), /) -> object: ...
    def fetchall(self) -> Sequence[Sequence[object]]: ...
    def fetchmany(self, size: int, /) -> Sequence[Sequence[object]]: ...
    def close(self) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class LedgerEntry(BaseModel):
    """One applied change and what happened to it at revert time."""

    model_config = STRICT
    table: str
    key: dict[str, Scalar]
    column: str
    before: JsonValue
    after: Scalar
    reverted: bool = False
    revert_ok: bool | None = None
    observed_before_revert: JsonValue = None
    drifted: bool = False
    note: str = ""


_DRIVERS = {"oracle": "oracledb", "mssql": "pyodbc"}


def connect(dialect: Dialect, dsn: str) -> Connection:
    """Open a connection. Oracle and MSSQL drivers are optional extras, imported only when asked for."""
    if dialect == "sqlite":
        return cast("Connection", sqlite3.connect(dsn))  # structurally compatible; sqlite3's overloaded cursor() defeats the subtype check
    try:
        module = importlib.import_module(_DRIVERS[dialect])
    except ImportError as exc:
        msg = f"driver for {dialect!r} is not installed; run: uv sync --extra {dialect}"
        raise HarnessError(msg) from exc
    conn: Connection = module.connect(dsn)  # the driver is untyped; the Protocol above is the adapter boundary
    return conn


def check_qa_target(dsn: str, markers: Sequence[str]) -> None:
    """Refuse a DSN that does not look like QA."""
    if markers and not any(m.lower() in dsn.lower() for m in markers):
        msg = f"DSN target {describe_target(dsn)!r} matches none of db.qa_markers {list(markers)}; refusing"
        raise HarnessError(msg)


def describe_target(dsn: str) -> str:
    """The DSN with credentials removed, safe to store in a manifest."""
    masked = re.sub(r"(?i)\b(pwd|password)=[^;]*", r"\1=***", dsn)
    return masked.rsplit("@", 1)[-1]


def jsonable(value: object) -> JsonValue:
    """Coerce a driver value into something JSON holds without losing what a revert needs."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex()
    return str(value)


def same(a: object, b: object) -> bool:
    """Equality that tolerates driver type differences: Decimal('0.1') vs 0.1, '1' vs 1."""
    ja, jb = jsonable(a), jsonable(b)
    if ja == jb:
        return True
    try:
        return float(str(ja)) == float(str(jb))
    except ValueError:
        return False


def _bind(value: JsonValue) -> Scalar:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return json.dumps(value)


def _ph(dialect: Dialect, n: int) -> str:
    return f":{n}" if dialect == "oracle" else "?"


def _where(dialect: Dialect, key: dict[str, Scalar], first: int) -> tuple[str, list[Scalar]]:
    clause = " AND ".join(f"{col} = {_ph(dialect, first + i)}" for i, col in enumerate(key))
    return clause, list(key.values())


def read_one(conn: Connection, dialect: Dialect, table: str, key: dict[str, Scalar], column: str) -> object:
    """The column's current value on exactly one row. Zero or several rows is a refusal."""
    where, params = _where(dialect, key, 1)
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {column} FROM {table} WHERE {where}", params)
        rows = cur.fetchall()
    finally:
        cur.close()
    if len(rows) != 1:
        msg = f"{table} where {key}: expected exactly 1 row, found {len(rows)}"
        raise HarnessError(msg)
    return rows[0][0]


def _update_one(cur: Cursor, dialect: Dialect, table: str, key: dict[str, Scalar], column: str, value: Scalar) -> None:
    where, params = _where(dialect, key, 2)
    cur.execute(f"UPDATE {table} SET {column} = {_ph(dialect, 1)} WHERE {where}", [value, *params])
    if cur.rowcount != 1:
        msg = f"UPDATE {table} where {key} touched {cur.rowcount} rows, expected 1; rolled back"
        raise HarnessError(msg)


def apply(conn: Connection, dialect: Dialect, changes: Sequence[Change]) -> list[LedgerEntry]:
    """Apply every change in one transaction, recording before-images. Any failure rolls back all of them."""
    entries: list[LedgerEntry] = []
    cur = conn.cursor()
    try:
        for change in changes:
            before = jsonable(read_one(conn, dialect, change.table, change.key, change.column))
            _update_one(cur, dialect, change.table, change.key, change.column, change.value)
            entries.append(LedgerEntry(table=change.table, key=change.key, column=change.column, before=before, after=change.value))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return entries


def revert(conn: Connection, dialect: Dialect, entries: Sequence[LedgerEntry]) -> list[LedgerEntry]:
    """Restore before-images in reverse order, in one transaction. Raises if anything fails; nothing is then reverted."""
    done: dict[int, LedgerEntry] = {}
    cur = conn.cursor()
    try:
        for i in reversed(range(len(entries))):
            entry = entries[i]
            if entry.reverted and entry.revert_ok:
                continue
            observed = jsonable(read_one(conn, dialect, entry.table, entry.key, entry.column))
            drifted = not same(observed, entry.after)
            if drifted:
                log.warning("%s.%s drifted since apply; restoring anyway", entry.table, entry.column, extra={"key": entry.key, "observed": observed})
            _update_one(cur, dialect, entry.table, entry.key, entry.column, _bind(entry.before))
            done[i] = LedgerEntry(
                table=entry.table,
                key=entry.key,
                column=entry.column,
                before=entry.before,
                after=entry.after,
                reverted=True,
                revert_ok=True,
                observed_before_revert=observed,
                drifted=drifted,
                note="value had changed since apply (someone else touched it?); restored to before-image" if drifted else "",
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return [done.get(i, e) for i, e in enumerate(entries)]


def mark_revert_failed(entries: Sequence[LedgerEntry], note: str) -> list[LedgerEntry]:
    return [e if e.reverted and e.revert_ok else LedgerEntry(table=e.table, key=e.key, column=e.column, before=e.before, after=e.after, reverted=False, revert_ok=False, note=note) for e in entries]


_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def query(conn: Connection, sql: str, limit: int) -> tuple[list[str], list[Sequence[object]]]:
    """Run one SELECT and return up to `limit` rows. The read-only account enforces this; the check only catches slips."""
    body = sql.strip().rstrip(";")
    if not _READ_ONLY.match(body) or ";" in body:
        msg = "only a single SELECT/WITH statement runs here; data changes go through a spec"
        raise HarnessError(msg)
    cur = conn.cursor()
    try:
        cur.execute(body)
        columns = [str(d[0]) for d in cur.description or []]
        rows = list(cur.fetchmany(limit))
    finally:
        cur.close()
    return columns, rows
