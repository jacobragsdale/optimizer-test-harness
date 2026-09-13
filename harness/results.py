"""Parquet reader for optimizer output: per-file summaries and ad hoc SQL through duckdb."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import duckdb

from harness import HarnessError


def parquet_files(root: Path) -> list[Path]:
    if root.is_file() and root.suffix == ".parquet":
        return [root]
    if root.is_dir():
        files = sorted(root.rglob("*.parquet"))
        if files:
            return files
    msg = f"no parquet files under {root}"
    raise HarnessError(msg)


def open_views(files: Sequence[Path]) -> tuple[duckdb.DuckDBPyConnection, dict[str, Path]]:
    """One duckdb view per file, named by file stem, so SQL can say `SELECT ... FROM holdings`."""
    con = duckdb.connect()
    views: dict[str, Path] = {}
    for path in files:
        base = re.sub(r"\W+", "_", path.stem).strip("_") or "t"
        name, n = base, 2
        while name in views:
            name, n = f"{base}_{n}", n + 1
        views[name] = path
        literal = str(path).replace("'", "''")
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{literal}')")
    return con, views


def query(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[list[str], list[tuple[object, ...]]]:
    result = con.execute(sql)
    columns = [str(d[0]) for d in result.description or []]
    rows: list[tuple[object, ...]] = result.fetchall()
    return columns, rows


def summarize(con: duckdb.DuckDBPyConnection, views: dict[str, Path]) -> str:
    blocks: list[str] = []
    for name, path in views.items():
        count = con.execute(f"SELECT count(*) FROM {name}").fetchone()
        blocks.append(f"## {name}: {count[0] if count else '?'} rows  ({path})")
        blocks.append(format_table(*query(con, f"SUMMARIZE {name}")))
        blocks.append("first rows:")
        blocks.append(format_table(*query(con, f"SELECT * FROM {name} LIMIT 5")))
        blocks.append("")
    return "\n".join(blocks)


def format_table(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    if not columns:
        return "(no columns)"
    cells = [[str(c) for c in columns], *([_cell(v) for v in row] for row in rows)]
    widths = [max(len(r[i]) for r in cells) for i in range(len(columns))]
    lines = [" | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in cells]
    lines.insert(1, "-+-".join("-" * w for w in widths))
    lines.append(f"({len(rows)} rows)")
    return "\n".join(lines)


def _cell(value: object) -> str:
    text = "NULL" if value is None else str(value)
    return text if len(text) <= 60 else text[:57] + "..."
