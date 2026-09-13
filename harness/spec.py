"""Test case spec: the structured form of one BA test case. The compile skill writes it; this module validates it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, JsonValue

from harness.config import STRICT, Config

Scalar = str | int | float | bool | None
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CASE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class Source(BaseModel):
    model_config = STRICT
    sheet: str
    row: int


class Portfolio(BaseModel):
    model_config = STRICT
    criteria: str
    ids: list[str]
    rationale: str


class Change(BaseModel):
    """Set one column on one row. The row is identified by the allowlisted key columns, nothing else."""

    model_config = STRICT
    table: str
    key: dict[str, Scalar]
    column: str
    value: Scalar


class RunRequest(BaseModel):
    model_config = STRICT
    params: dict[str, JsonValue]
    """Sent verbatim as the JSON body of the submit call."""


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
    portfolio: Portfolio
    changes: list[Change]
    run: RunRequest
    expected: Expected


def load_spec(path: Path) -> Spec:
    return Spec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def check_changes(spec: Spec, config: Config) -> list[str]:
    """Every reason the spec may not run. Empty means it may. Identifiers reach SQL only after passing this."""
    problems: list[str] = []
    if not _CASE_ID.match(spec.case_id):
        problems.append("case_id must be letters, digits, '.', '_' or '-' (it names a folder)")
    for i, change in enumerate(spec.changes, 1):
        where = f"changes[{i}] {change.table}.{change.column}"
        allowed = config.allowed_table(change.table)
        if allowed is None:
            problems.append(f"{where}: table is not in the [[allow]] list")
            continue
        if not all(_IDENT.match(name) for name in (change.table, change.column, *change.key)):
            problems.append(f"{where}: identifiers must be plain names (letters, digits, underscore)")
        if change.column.upper() not in {c.upper() for c in allowed.columns}:
            problems.append(f"{where}: column not allowed; allowed: {', '.join(allowed.columns)}")
        if {k.upper() for k in change.key} != {k.upper() for k in allowed.key}:
            problems.append(f"{where}: key must be exactly {', '.join(allowed.key)}")
        if any(v is None for v in change.key.values()):
            problems.append(f"{where}: key values may not be null")
    return problems
