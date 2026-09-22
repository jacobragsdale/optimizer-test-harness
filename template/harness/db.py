"""Database adapter: read-only queries, allowlisted single-row writes with a before-image ledger, and revert.

Identifiers are interpolated into SQL only after `harness.spec.check_spec` matched them against the allowlist, or when
they are column names the database itself returned. Values always travel as bind parameters.

Every revert is idempotent: re-running it after a partial failure, a crash, or a commit that never happened restores
the same state and says what it found.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol, cast

from pydantic import BaseModel, Field, JsonValue

from harness import HarnessError
from harness.config import STRICT, VAR, Config, Dialect
from harness.spec import IDENT, DbInsert, DbStep, DbUpdate, Scalar, check_read_only, db_key

log = logging.getLogger(__name__)


class Cursor(Protocol):
    """The DB-API 2.0 subset the harness uses."""

    @property
    def rowcount(self) -> int: ...
    @property
    def description(self) -> Sequence[Sequence[object]] | None: ...
    def execute(self, sql: str, params: Sequence[object] = (), /) -> object: ...
    def fetchall(self) -> Sequence[Sequence[object]]: ...
    def fetchmany(self, size: int, /) -> Sequence[Sequence[object]]: ...
    def close(self) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class LedgerEntry(BaseModel):
    """One applied setup step and what happened to it at revert time.

    kind "db": `op` is update/insert/delete; `before` and `after` are column values (a delete's `before` is the whole row).
    kind "api": `op` is the operation, `vars` what it captured (the undo's inputs). `http_status` stays None while the
    call is in flight, so a crash mid-call is visible as an entry whose outcome is unknown.
    """

    model_config = STRICT
    kind: Literal["db", "api"]
    op: str
    table: str = ""
    key: dict[str, Scalar] = Field(default_factory=dict)
    before: dict[str, JsonValue] = Field(default_factory=dict)
    after: dict[str, JsonValue] = Field(default_factory=dict)
    request: JsonValue = None
    undo: str = ""
    vars: dict[str, JsonValue] = Field(default_factory=dict)
    http_status: int | None = None
    reverted: bool = False
    revert_ok: bool | None = None
    observed_before_revert: dict[str, JsonValue] = Field(default_factory=dict)
    drifted: bool = False
    note: str = ""

    @property
    def done(self) -> bool:
        return self.reverted and self.revert_ok is True


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


def check_qa_target(target: str, markers: Sequence[str], what: str = "DSN") -> None:
    """Refuse a DSN or URL that does not look like QA."""
    if markers and not any(m.lower() in target.lower() for m in markers):
        msg = f"{what} {describe_target(target)!r} matches none of db.qa_markers {list(markers)}; refusing"
        raise HarnessError(msg)


def describe_target(dsn: str) -> str:
    """The DSN with credentials removed, safe to store in a manifest."""
    masked = re.sub(r"(?i)\b(pwd|password)=[^;]*", r"\1=***", dsn)
    return masked.rsplit("@", 1)[-1]


def jsonable(value: object) -> JsonValue:
    """A driver value as JSON. Types JSON cannot hold are tagged, so `bind` can hand the driver the original type back."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, bytes | bytearray):
        return {"$type": "bytes", "value": bytes(value).hex()}
    return str(value)


_DECODE: dict[str, Callable[[str], object]] = {"decimal": Decimal, "datetime": datetime.fromisoformat, "date": date.fromisoformat, "bytes": bytes.fromhex}


def bind(value: JsonValue) -> object:
    """The inverse of `jsonable`: what to pass the driver to write the value back."""
    if isinstance(value, dict) and isinstance(value.get("$type"), str) and isinstance(value.get("value"), str):
        return _DECODE[str(value["$type"])](str(value["value"]))
    if isinstance(value, dict | list):
        return json.dumps(value)
    return value


def plain(value: object) -> Scalar:
    """A ledger value as a plain scalar, for comparisons, reports and CSV snapshots."""
    if isinstance(value, dict) and "$type" in value:
        return str(value.get("value"))
    if isinstance(value, dict | list):
        return json.dumps(value, default=str)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def same(observed: object, expected: JsonValue) -> bool:
    """Equality that tolerates driver type differences: Decimal('0.1') vs 0.1, '1' vs 1, a date vs its ISO string."""
    a, b = plain(jsonable(observed)), plain(expected)
    if a == b:
        return True
    try:
        return float(str(a)) == float(str(b))
    except ValueError:
        return False


def _ph(dialect: Dialect, n: int) -> str:
    return f":{n}" if dialect == "oracle" else "?"


def _where(dialect: Dialect, key: Mapping[str, object], first: int) -> tuple[str, list[object]]:
    clause = " AND ".join(f"{col} = {_ph(dialect, first + i)}" for i, col in enumerate(key))
    return clause, list(key.values())


def _checked(names: Sequence[str]) -> list[str]:
    if bad := [n for n in names if not IDENT.match(n)]:
        msg = f"refusing unusual identifiers {bad}"
        raise HarnessError(msg)
    return list(names)


def select_row(conn: Connection, dialect: Dialect, table: str, key: Mapping[str, object], columns: Sequence[str] | None) -> dict[str, object] | None:
    """The row's columns (all of them when `columns` is None), None when there is no row. Several rows is a refusal."""
    where, params = _where(dialect, key, 1)
    cols = ", ".join(_checked(columns)) if columns is not None else "*"
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {cols} FROM {table} WHERE {where}", params)
        rows = cur.fetchall()
        names = list(columns) if columns is not None else [str(d[0]) for d in cur.description or []]
    finally:
        cur.close()
    if len(rows) > 1:
        msg = f"{table} where {dict(key)}: expected at most 1 row, found {len(rows)}; the key does not identify a row"
        raise HarnessError(msg)
    return dict(zip(names, rows[0], strict=True)) if rows else None


def _one_row(conn: Connection, dialect: Dialect, table: str, key: Mapping[str, object], columns: Sequence[str] | None) -> dict[str, object]:
    row = select_row(conn, dialect, table, key, columns)
    if row is None:
        msg = f"{table} where {dict(key)}: expected exactly 1 row, found 0"
        raise HarnessError(msg)
    return row


def _execute_one(cur: Cursor, sql: str, params: Sequence[object], what: str) -> None:
    cur.execute(sql, params)
    if cur.rowcount != 1:
        msg = f"{what} touched {cur.rowcount} rows, expected 1; rolled back"
        raise HarnessError(msg)


def _update(cur: Cursor, dialect: Dialect, table: str, key: Mapping[str, object], values: Mapping[str, object]) -> None:
    sets = ", ".join(f"{c} = {_ph(dialect, i)}" for i, c in enumerate(_checked(list(values)), 1))
    where, params = _where(dialect, key, len(values) + 1)
    _execute_one(cur, f"UPDATE {table} SET {sets} WHERE {where}", [*values.values(), *params], f"UPDATE {table} where {dict(key)}")


def _insert(cur: Cursor, dialect: Dialect, table: str, values: Mapping[str, object]) -> None:
    cols = _checked(list(values))
    marks = ", ".join(_ph(dialect, i) for i in range(1, len(cols) + 1))
    _execute_one(cur, f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", list(values.values()), f"INSERT INTO {table}")


def _delete(cur: Cursor, dialect: Dialect, table: str, key: Mapping[str, object]) -> None:
    where, params = _where(dialect, key, 1)
    _execute_one(cur, f"DELETE FROM {table} WHERE {where}", params, f"DELETE FROM {table} where {dict(key)}")


def _apply_one(conn: Connection, cur: Cursor, config: Config, step: DbStep) -> LedgerEntry:
    dialect = config.db.dialect
    allowed = config.allowed_table(step.table)
    if allowed is None:  # check_spec already refused this; kept so no path reaches SQL unchecked
        msg = f"{step.table} is not in the [[allow]] list"
        raise HarnessError(msg)
    key = db_key(step, allowed)
    if isinstance(step, DbUpdate):
        before = _one_row(conn, dialect, step.table, key, list(step.set))
        _update(cur, dialect, step.table, key, step.set)
        return LedgerEntry(kind="db", op="update", table=step.table, key=key, before={c: jsonable(v) for c, v in before.items()}, after=dict(step.set))
    if isinstance(step, DbInsert):
        if select_row(conn, dialect, step.table, key, list(key)) is not None:
            msg = f"{step.table} where {key}: row already exists; an insert needs a key that is not in use"
            raise HarnessError(msg)
        _insert(cur, dialect, step.table, step.values)
        return LedgerEntry(kind="db", op="insert", table=step.table, key=key, after=dict(step.values))
    row = _one_row(conn, dialect, step.table, key, None)
    _delete(cur, dialect, step.table, key)
    return LedgerEntry(kind="db", op="delete", table=step.table, key=key, before={c: jsonable(v) for c, v in row.items()})


def apply(conn: Connection, config: Config, steps: Sequence[DbStep], on_staged: Callable[[list[LedgerEntry]], None]) -> list[LedgerEntry]:
    """Apply the steps in one transaction, recording before-images. Any failure rolls back all of them.

    `on_staged` gets the ledger after the writes and before the commit, so it can persist the before-images first:
    a crash can then never leave committed changes without a record of how to undo them.
    """
    entries: list[LedgerEntry] = []
    cur = conn.cursor()
    try:
        entries.extend(_apply_one(conn, cur, config, step) for step in steps)
        on_staged(entries)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return entries


def _revert_one(conn: Connection, cur: Cursor, dialect: Dialect, e: LedgerEntry) -> LedgerEntry:
    if e.op == "update":
        observed = _one_row(conn, dialect, e.table, e.key, list(e.before))
        drifted = not all(same(observed[c], e.after[c]) for c in e.before)
        _update(cur, dialect, e.table, e.key, {c: bind(v) for c, v in e.before.items()})
        note = "value had changed since apply (someone else touched it, or the apply never committed); restored to before-image" if drifted else ""
    elif e.op == "insert":
        found = select_row(conn, dialect, e.table, e.key, list(e.after))
        if found is None:
            return e.model_copy(update={"reverted": True, "revert_ok": True, "note": "inserted row was already gone (or the apply never committed)"})
        observed = found
        drifted = not all(same(observed[c], e.after[c]) for c in e.after)
        _delete(cur, dialect, e.table, e.key)
        note = "inserted row had changed since apply; deleted anyway" if drifted else ""
    else:
        found = select_row(conn, dialect, e.table, e.key, list(e.before))
        if found is not None:
            if all(same(found[c], e.before[c]) for c in e.before):
                return e.model_copy(update={"reverted": True, "revert_ok": True, "note": "deleted row was already back (or the apply never committed)"})
            msg = f"{e.table} where {e.key}: a different row now holds the key of the deleted row; resolve by hand"
            raise HarnessError(msg)
        observed = {}
        drifted = False
        _insert(cur, dialect, e.table, {c: bind(v) for c, v in e.before.items()})
        note = ""
    if drifted:
        log.warning("%s drifted since apply", e.table, extra={"key": e.key})
    return e.model_copy(update={"reverted": True, "revert_ok": True, "observed_before_revert": {c: jsonable(v) for c, v in observed.items()}, "drifted": drifted, "note": note})


def revert(conn: Connection, dialect: Dialect, entries: Sequence[LedgerEntry]) -> list[LedgerEntry]:
    """Undo DB entries in reverse order, in one transaction. Raises if anything fails; nothing is then reverted."""
    done: dict[int, LedgerEntry] = {}
    cur = conn.cursor()
    try:
        for i in reversed(range(len(entries))):
            if not entries[i].done:
                done[i] = _revert_one(conn, cur, dialect, entries[i])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return [done.get(i, e) for i, e in enumerate(entries)]


def mark_revert_failed(entries: Sequence[LedgerEntry], note: str) -> list[LedgerEntry]:
    return [e if e.done else e.model_copy(update={"reverted": False, "revert_ok": False, "note": note}) for e in entries]


def query(conn: Connection, sql: str, limit: int, params: Sequence[object] = ()) -> tuple[list[str], list[Sequence[object]]]:
    """Run one SELECT and return up to `limit` rows, then roll back whatever a statement might have slipped past the check."""
    body = check_read_only(sql)
    cur = conn.cursor()
    try:
        cur.execute(body, params)
        columns = [str(d[0]) for d in cur.description or []]
        rows = list(cur.fetchmany(limit))
    finally:
        cur.close()
        conn.rollback()
    return columns, rows


def bind_vars(dialect: Dialect, sql: str, values: Mapping[str, JsonValue]) -> tuple[str, list[object]]:
    """Replace each `{var}` with a bind placeholder and return the matching parameters. Unknown variables are a refusal."""
    params: list[object] = []

    def placeholder(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            msg = f"{{{name}}} was never captured in this case"
            raise HarnessError(msg)
        params.append(bind(values[name]))
        return _ph(dialect, len(params))

    return VAR.sub(placeholder, sql), params
