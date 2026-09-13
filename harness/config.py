"""Configuration boundary: harness.toml for structure, environment variables for secrets."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

STRICT = ConfigDict(strict=True, extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False)
RECORD = ConfigDict(strict=True, extra="forbid", validate_assignment=True, validate_default=True, allow_inf_nan=False)
"""For accumulating records (manifest, run) that are mutated through explicit, validated assignment."""

Dialect = Literal["sqlite", "oracle", "mssql"]


class AllowedTable(BaseModel):
    """A table a test may update: only these key columns identify a row, only these columns may change."""

    model_config = STRICT
    table: str
    key: list[str]
    columns: list[str]


class DbConfig(BaseModel):
    model_config = STRICT
    dialect: Dialect
    qa_markers: list[str]
    """The DSN, lowercased, must contain one of these or the CLI refuses to connect. Empty disables the guard."""


class ApiConfig(BaseModel):
    model_config = STRICT
    submit_url: str
    status_url: str
    run_url: str
    run_id_field: str
    status_field: str
    output_path_field: str
    terminal_statuses: list[str]
    success_statuses: list[str]
    poll_seconds: float
    timeout_seconds: float


class PathsConfig(BaseModel):
    model_config = STRICT
    runs: str


class Config(BaseModel):
    model_config = STRICT
    db: DbConfig
    allow: list[AllowedTable]
    api: ApiConfig
    paths: PathsConfig

    def allowed_table(self, table: str) -> AllowedTable | None:
        return next((a for a in self.allow if a.table.upper() == table.upper()), None)


def load_config(path: Path) -> Config:
    return Config.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


class Settings(BaseSettings):
    """Secrets, from HARNESS_* environment variables. See .env.example."""

    model_config = SettingsConfigDict(env_prefix="HARNESS_", strict=True, extra="forbid", frozen=True, validate_default=True, allow_inf_nan=False)
    db_dsn: str
    db_ro_dsn: str | None = None
    api_token: str | None = None
