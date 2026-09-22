"""Test case spec: the structured form of one BA test case. The compile skill writes it; this module validates it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Discriminator, JsonValue, Tag

from harness import HarnessError
from harness.config import STRICT, VAR, AllowedTable, Config, variables

Scalar = str | int | float | bool | None
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CASE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class Source(BaseModel):
    model_config = STRICT
    sheet: str
    row: int


class Subject(BaseModel):
    """What the test is about in the app's terms (an account, a product, a portfolio), and why these ones."""

    model_config = STRICT
    criteria: str
    ids: list[str]
    rationale: str


class DbUpdate(BaseModel):
    """Set columns on one existing row, identified by the full allowlisted key."""

    model_config = STRICT
    db: Literal["update"]
    table: str
    key: dict[str, Scalar]
    set: dict[str, Scalar]


class DbInsert(BaseModel):
    """Add one row. `values` must include the full allowlisted key, and no row with that key may exist yet."""

    model_config = STRICT
    db: Literal["insert"]
    table: str
    values: dict[str, Scalar]


class DbDelete(BaseModel):
    """Remove one row, identified by the full allowlisted key. The whole row is kept so the revert can put it back."""

    model_config = STRICT
    db: Literal["delete"]
    table: str
    key: dict[str, Scalar]


class ApiStep(BaseModel):
    """Call a named operation from harness.toml. `body` replaces the operation's default body; strings may use `{var}`."""

    model_config = STRICT
    api: str
    body: JsonValue = None


DbStep = DbUpdate | DbInsert | DbDelete


def _step_kind(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("db", "api"))
    return str(getattr(value, "db", "api"))


SetupStep = Annotated[Annotated[DbUpdate, Tag("update")] | Annotated[DbInsert, Tag("insert")] | Annotated[DbDelete, Tag("delete")] | Annotated[ApiStep, Tag("api")], Discriminator(_step_kind)]


class Collect(BaseModel):
    """A read-only query snapshotted to `<name>.csv` in the run folder after the run and before the revert.

    `{var}` in the SQL becomes a bind parameter holding the captured value, so write `WHERE JOB_ID = {job_id}`, unquoted.
    """

    model_config = STRICT
    name: str
    sql: str


class Expected(BaseModel):
    model_config = STRICT
    outcome: Literal["success", "failure"]
    text: str
    evaluation_plan: list[str]
    """Written before the run: what to look at in the output and what it should show."""


class Spec(BaseModel):
    model_config = STRICT
    case_id: str
    title: str
    source: Source
    description: str
    interpretation: str
    subject: Subject
    setup: list[SetupStep]
    run: list[ApiStep]
    collect: list[Collect]
    expected: Expected


def load_spec(path: Path) -> Spec:
    return Spec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def db_key(step: DbStep, allowed: AllowedTable) -> dict[str, Scalar]:
    """The row key of a DB step. For an insert it is taken from `values`, by the allowlisted key columns."""
    if isinstance(step, DbInsert):
        by_upper = {k.upper(): v for k, v in step.values.items()}
        return {k: by_upper.get(k.upper()) for k in allowed.key}
    return step.key


def check_spec(spec: Spec, config: Config) -> list[str]:
    """Every reason the spec may not run. Empty means it may. Identifiers reach SQL only after passing this."""
    problems: list[str] = []
    if not _CASE_ID.match(spec.case_id):
        problems.append("case_id must be letters, digits, '.', '_' or '-' (it names a folder)")
    known = {"base_url"} | (_op_captures(config, config.api.login) if config.api.login else set())
    for i, step in enumerate(spec.setup, 1):
        where = f"setup[{i}]"
        if isinstance(step, ApiStep):
            problems += _check_api(step, where, config, known, setup=True)
        else:
            problems += _check_db(step, f"{where} {step.db} {step.table}", config)
    for i, step in enumerate(spec.run, 1):
        problems += _check_api(step, f"run[{i}]", config, known, setup=False)
    names = [c.name for c in spec.collect]
    for i, c in enumerate(spec.collect, 1):
        where = f"collect[{i}] {c.name}"
        if not IDENT.match(c.name) or names.count(c.name) > 1:
            problems.append(f"{where}: name must be a unique plain name (it becomes the view name)")
        if missing := variables(c.sql) - known:
            problems.append(f"{where}: uses {sorted(missing)}, which no earlier step captures")
        try:
            check_read_only(VAR.sub("NULL", c.sql))
        except HarnessError as exc:
            problems.append(f"{where}: {exc}")
    referenced = json.dumps([s.model_dump() for s in [*spec.setup, *spec.run, *spec.collect]])
    problems += [f"subject {sid!r} is in subject.ids but no setup, run or collect step mentions it" for sid in spec.subject.ids if sid not in referenced]
    return problems


def _op_captures(config: Config, name: str) -> set[str]:
    op = config.op(name)
    return set(op.capture) if op else set()


def _check_api(step: ApiStep, where: str, config: Config, known: set[str], *, setup: bool) -> list[str]:
    """Checks one API step and adds what it captures to `known`, so later steps may use it."""
    op = config.op(step.api)
    if op is None:
        return [f"{where}: operation {step.api!r} is not defined in harness.toml"]
    problems = []
    if setup and op.undo is None:
        problems.append(f"{where}: operation {op.name!r} has no undo, so it cannot be used as setup")
    used = variables(op.path) | variables(op.body if step.body is None else step.body)
    if missing := used - known:
        problems.append(f"{where} {op.name}: uses {sorted(missing)}, which no earlier step captures")
    known |= set(op.capture)
    return problems


def _check_db(step: DbStep, where: str, config: Config) -> list[str]:
    allowed = config.allowed_table(step.table)
    if allowed is None:
        return [f"{where}: table is not in the [[allow]] list"]
    if step.db not in allowed.ops:
        return [f"{where}: {step.db} is not allowed on this table; allowed: {', '.join(allowed.ops)}"]
    problems: list[str] = []
    columns = list(step.set) if isinstance(step, DbUpdate) else list(step.values) if isinstance(step, DbInsert) else []
    keys = list(step.values) if isinstance(step, DbInsert) else list(step.key)
    if not all(IDENT.match(name) for name in (step.table, *columns, *keys)):
        problems.append(f"{where}: identifiers must be plain names (letters, digits, underscore)")
    upper_key = {k.upper() for k in allowed.key}
    writable = {c.upper() for c in allowed.columns}
    if isinstance(step, DbUpdate):
        if not step.set:
            problems.append(f"{where}: set is empty")
        if bad := [c for c in step.set if c.upper() not in writable]:
            problems.append(f"{where}: columns {bad} not allowed; allowed: {', '.join(allowed.columns)}")
    if isinstance(step, DbInsert):
        if bad := [c for c in step.values if c.upper() not in writable | upper_key]:
            problems.append(f"{where}: columns {bad} not allowed; allowed: {', '.join(allowed.key + allowed.columns)}")
        if not upper_key <= {c.upper() for c in step.values}:
            problems.append(f"{where}: values must include the key {', '.join(allowed.key)}")
    elif {k.upper() for k in step.key} != upper_key:
        problems.append(f"{where}: key must be exactly {', '.join(allowed.key)}")
    if any(v is None for v in db_key(step, allowed).values()):
        problems.append(f"{where}: key values may not be null")
    return problems


_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_WRITES = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|exec|execute|call|into)\b", re.IGNORECASE)
"""Catches `WITH x AS (...) DELETE FROM x` (MSSQL), `SELECT ... INTO`, `FOR UPDATE`. A keyword in a string literal is a false refusal."""


def check_read_only(sql: str) -> str:
    """The statement without a trailing semicolon, or a refusal. The read-only account enforces this; the check catches slips."""
    body = sql.strip().rstrip(";")
    write = _WRITES.search(body)
    if not _READ_ONLY.match(body) or ";" in body or write:
        found = f" (found {write.group(0)!r}; if it is inside a string literal, rephrase)" if write else ""
        msg = f"only a single read-only SELECT/WITH statement runs here{found}; data changes go through a spec"
        raise HarnessError(msg)
    return body
