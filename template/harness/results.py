"""Evidence reader: per-file summaries and ad hoc SQL through duckdb over a run's saved responses, collect snapshots and outputs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import duckdb

from harness import HarnessError

_READERS = {".parquet": "read_parquet", ".csv": "read_csv_auto", ".json": "read_json_auto"}


def data_files(roots: Sequence[Path]) -> list[Path]:
    """Every parquet, csv and json file at or under the roots, except manifests."""
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files += sorted(p for p in root.rglob("*") if p.suffix in _READERS and p.name != "manifest.json")
    files = [f for f in files if f.suffix in _READERS]
    if not files:
        msg = f"no parquet, csv or json files under {', '.join(map(str, roots))}"
        raise HarnessError(msg)
    return files


def open_views(files: Sequence[Path]) -> tuple[duckdb.DuckDBPyConnection, dict[str, Path], list[str]]:
    """One duckdb view per file, named by file stem, so SQL can say `SELECT ... FROM get_prices`. Also returns skipped files."""
    con = duckdb.connect()
    views: dict[str, Path] = {}
    skipped: list[str] = []
    for path in files:
        base = re.sub(r"\W+", "_", path.stem).strip("_") or "t"
        if base[0].isdigit():
            base = f"t_{base}"  # an unquoted SQL identifier cannot start with a digit
        name, n = base, 2
        while name in views:
            name, n = f"{base}_{n}", n + 1
        literal = str(path).replace("'", "''")
        try:
            con.execute(f"CREATE VIEW {name} AS SELECT * FROM {_READERS[path.suffix]}('{literal}')")
        except duckdb.Error as exc:
            skipped.append(f"{path.name}: {str(exc).splitlines()[0]}")
            continue
        views[name] = path
    return con, views, skipped


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
