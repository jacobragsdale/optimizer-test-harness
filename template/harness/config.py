"""Configuration boundary: harness.toml for structure, environment variables for secrets."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

STRICT = ConfigDict(strict=True, extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False)
RECORD = ConfigDict(strict=True, extra="forbid", validate_assignment=True, validate_default=True, allow_inf_nan=False)
"""For accumulating records (manifest, step) that are mutated through explicit, validated assignment."""

Dialect = Literal["sqlite", "oracle", "mssql"]
DbOp = Literal["update", "insert", "delete"]
Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
VAR = re.compile(r"(?<!\$)\{(\w+)\}")
"""`{name}` in an operation path, a request body string, a collect query or the report link is a case variable."""


class AllowedTable(BaseModel):
    """A table a test may write: rows are identified by `key` only, and only `columns` may be set or filled."""

    model_config = STRICT
    table: str
    key: list[str]
    columns: list[str]
    ops: list[DbOp]


class DbConfig(BaseModel):
    model_config = STRICT
    dialect: Dialect
    qa_markers: list[str]
    """The DB DSN and api.base_url, lowercased, must each contain one of these or the CLI refuses. Empty disables the guard."""


class Poll(BaseModel):
    """Repeat the call until `field` reaches one of `until`; the step succeeded if it ended in one of `success`."""

    model_config = STRICT
    field: str
    until: list[str]
    success: list[str]
    every_seconds: float
    timeout_seconds: float


class Operation(BaseModel):
    """One API call a spec may make, by name. Specs never carry a method or a path of their own."""

    model_config = STRICT
    name: str
    method: Method
    path: str
    body: JsonValue = None
    """Default request body; a spec step's `body` replaces it. Strings may use `{var}` and `${ENV}`."""
    capture: dict[str, str] = Field(default_factory=dict)
    """Case variable -> dot path into the (final) JSON response, e.g. `data.id` or `items.0.id`."""
    poll: Poll | None = None
    undo: str | None = None
    """The operation that reverses this one, called with this operation's captured variables. Required for setup use."""


class ApiConfig(BaseModel):
    model_config = STRICT
    base_url: str
    headers: dict[str, str] = Field(default_factory=dict)
    """Sent on every call. Values may use `${ENV}`; only the template is ever written to a manifest."""
    login: str | None = None
    """Operation called once at the start of every case and every manual revert. Cookies it sets are kept for the case."""
    timeout_seconds: float = 60.0


class OutputsConfig(BaseModel):
    model_config = STRICT
    paths: list[str] = Field(default_factory=list)
    """Files or folders the app writes (parquet, csv, json), as templates over case variables, e.g. `{output_path}`."""


class ReportConfig(BaseModel):
    model_config = STRICT
    link: str | None = None
    """The page BAs open for a run, e.g. `{base_url}/ui/jobs/{job_id}`."""


class PathsConfig(BaseModel):
    model_config = STRICT
    runs: str


class Config(BaseModel):
    model_config = STRICT
    db: DbConfig
    allow: list[AllowedTable] = Field(default_factory=list)
    api: ApiConfig
    operation: list[Operation] = Field(default_factory=list)
    outputs: OutputsConfig = Field(default_factory=OutputsConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    paths: PathsConfig

    @model_validator(mode="after")
    def _references(self) -> Self:
        names = [op.name for op in self.operation]
        problems = [f"operation {n!r} is defined twice" for n in sorted({n for n in names if names.count(n) > 1})]
        if self.api.login is not None and self.api.login not in names:
            problems.append(f"api.login names unknown operation {self.api.login!r}")
        for op in self.operation:
            undo = self.op(op.undo) if op.undo else None
            if op.undo and undo is None:
                problems.append(f"operation {op.name!r}: undo names unknown operation {op.undo!r}")
            if undo is not None and (missing := _vars_in(undo.path, undo.body) - set(op.capture)):
                problems.append(f"operation {op.name!r}: its undo {undo.name!r} uses {sorted(missing)}, which {op.name!r} does not capture")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def allowed_table(self, table: str) -> AllowedTable | None:
        return next((a for a in self.allow if a.table.upper() == table.upper()), None)

    def op(self, name: str) -> Operation | None:
        return next((o for o in self.operation if o.name == name), None)


def _vars_in(*values: JsonValue) -> set[str]:
    found: set[str] = set()
    for v in values:
        found |= variables(v)
    return found


def variables(value: JsonValue) -> set[str]:
    """Every `{var}` used anywhere in a string, list or dict value."""
    if isinstance(value, str):
        return set(VAR.findall(value))
    if isinstance(value, list):
        return _vars_in(*value)
    if isinstance(value, dict):
        return _vars_in(*value.values())
    return set()


def load_config(path: Path) -> Config:
    if not path.exists():
        msg = f"{path} not found. For a new app, run the harness-setup skill; for the local demo, pass --config demo/harness.toml"
        raise FileNotFoundError(msg)
    return Config.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


class Settings(BaseSettings):
    """Database DSNs, from HARNESS_* environment variables. API secrets are `${ENV}` references in harness.toml. See .env.example."""

    model_config = SettingsConfigDict(env_prefix="HARNESS_", strict=True, extra="ignore", frozen=True, validate_default=True, allow_inf_nan=False)
    db_dsn: str
    db_ro_dsn: str | None = None
