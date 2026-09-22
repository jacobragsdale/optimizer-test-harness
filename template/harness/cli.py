"""The harness CLI. Every side effect the skills need lives behind a subcommand here; stdout is the product."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from harness import HarnessError
from harness.config import Config, Settings, load_config
from harness.db import LedgerEntry, check_qa_target, connect, query
from harness.report import dump_workbook, render
from harness.results import data_files, format_table, open_views, summarize
from harness.results import query as data_query
from harness.run import Manifest, clear_marker, load_manifest, manifest_path, output_paths, pending_revert, plan_text, record_verdict, revert_manifest, run_case
from harness.spec import check_spec, load_spec

Command = Callable[[argparse.Namespace, Config, Path], int]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        config = load_config(Path(args.config))
        runs_dir = Path(config.paths.runs)
        pending = pending_revert(runs_dir)
        if pending is not None:
            print(f"!! UNREVERTED CHANGES are sitting in QA from {pending}\n!! run: harness revert {pending} --yes", file=sys.stderr)
        command: Command = args.func
        return command(args, config, runs_dir)
    except (HarnessError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_status(_args: argparse.Namespace, _config: Config, runs_dir: Path) -> int:
    pending = pending_revert(runs_dir)
    print(f"pending revert: {pending or 'none'}")
    manifests = _manifests(runs_dir)[-20:]
    if not manifests:
        print("no runs yet")
        return 0
    rows = [(m.case_id, m.started_at.strftime("%Y-%m-%d %H:%M"), m.verdict.result if m.verdict else "-", m.outcome, m.all_reverted, len(m.errors), str(p.parent)) for p, m in manifests]
    print(format_table(["case", "started_utc", "verdict", "outcome", "reverted", "errors", "run"], rows))
    return 0


def cmd_dump_workbook(args: argparse.Namespace, _config: Config, _runs_dir: Path) -> int:
    print(dump_workbook(Path(args.workbook)))
    return 0


def cmd_validate(args: argparse.Namespace, config: Config, _runs_dir: Path) -> int:
    bad = 0
    for raw in args.spec:
        spec = load_spec(Path(raw))
        problems = check_spec(spec, config)
        print(f"{raw}: {'OK' if not problems else 'NOT RUNNABLE'}")
        for p in problems:
            print(f"  - {p}")
        bad += bool(problems)
    return 1 if bad else 0


def cmd_sql(args: argparse.Namespace, config: Config, _runs_dir: Path) -> int:
    settings = Settings()
    dsn = settings.db_ro_dsn or settings.db_dsn
    if settings.db_ro_dsn is None:
        print("warning: HARNESS_DB_RO_DSN not set; using the read-write DSN (read-only is not enforced by grants)", file=sys.stderr)
    check_qa_target(dsn, config.db.qa_markers)
    conn = connect(config.db.dialect, dsn)
    try:
        columns, rows = query(conn, args.sql, args.limit)
    finally:
        conn.close()
    print(format_table(columns, rows))
    return 0


def cmd_run_case(args: argparse.Namespace, config: Config, runs_dir: Path) -> int:
    spec = load_spec(Path(args.spec))
    problems = check_spec(spec, config)
    if problems:
        msg = "spec is not runnable:\n  " + "\n  ".join(problems)
        raise HarnessError(msg)
    settings = Settings()
    if not args.yes:
        check_qa_target(settings.db_dsn, config.db.qa_markers)
        check_qa_target(config.api.base_url, config.db.qa_markers, "api.base_url")
        conn = connect(config.db.dialect, settings.db_dsn)
        try:
            print(plan_text(spec, config, conn))
        finally:
            conn.close()
        print("\nNothing has been changed. Show this plan to the user; re-run with --yes only after they say go.")
        return 0
    manifest, path = run_case(spec, config, settings, runs_dir, progress=lambda line: print(line, flush=True))
    print(f"\nrun: {path.parent}")
    print(f"outcome: {manifest.outcome}  run_ok: {manifest.run_ok}  link: {manifest.link or '-'}")
    for err in manifest.errors:
        print(f"ERROR: {err}")
    print(f"reverted: {manifest.all_reverted}")
    return 1 if manifest.errors else 0


def cmd_revert(args: argparse.Namespace, config: Config, runs_dir: Path) -> int:
    path = manifest_path(Path(args.manifest))
    if not path.exists() and clear_marker(runs_dir, path):  # killed before its first save, so before any change was applied
        print(f"{path} was never written, so nothing was applied; cleared the PENDING_REVERT marker")
        return 0
    manifest = load_manifest(path)
    todo = [e for e in manifest.ledger if not e.done]
    if not todo:
        cleared = clear_marker(runs_dir, path)
        print("nothing to revert; every setup step is already undone" + ("; cleared the PENDING_REVERT marker" if cleared else ""))
        return 0
    for e in reversed(todo):
        print(f"  {_describe(e)}")
    if not args.yes:
        print("\nNothing has been changed. Re-run with --yes to undo these.")
        return 0
    manifest = revert_manifest(path, config, Settings(), runs_dir)
    print(f"reverted: {manifest.all_reverted}")
    return 0 if manifest.all_reverted else 1


def _describe(e: LedgerEntry) -> str:
    if e.kind == "api":
        if e.http_status is None:
            return f"api {e.op}: interrupted mid-call, outcome unknown; check the app by hand"
        return f"api {e.op}: call {e.undo} with {e.vars}"
    if e.op == "update":
        return f"db update {e.table} {e.key}: restore {e.before}"
    if e.op == "insert":
        return f"db insert {e.table} {e.key}: delete the inserted row"
    return f"db delete {e.table} {e.key}: re-insert {e.before}"


def cmd_results(args: argparse.Namespace, config: Config, _runs_dir: Path) -> int:
    target = Path(args.target)
    if (target.is_dir() and (target / "manifest.json").exists()) or target.name == "manifest.json":
        path = manifest_path(target)
        roots = [path.parent, *output_paths(load_manifest(path), config)]
    else:
        roots = [target]
    con, views, skipped = open_views(data_files(roots))
    try:
        if args.sql:
            print(format_table(*data_query(con, args.sql)))
        else:
            print(f"views: {', '.join(views)}\n")
            print(summarize(con, views))
        for s in skipped:
            print(f"skipped {s}", file=sys.stderr)
    finally:
        con.close()
    return 0


def cmd_verdict(args: argparse.Namespace, _config: Config, _runs_dir: Path) -> int:
    manifest = record_verdict(Path(args.manifest), args.result, args.evidence, args.notes)
    print(f"{manifest.case_id}: {args.result} recorded")
    return 0


def cmd_report(args: argparse.Namespace, _config: Config, runs_dir: Path) -> int:
    manifests = _manifests(Path(args.runs) if args.runs else runs_dir)
    if not manifests:
        msg = "no manifests to report on"
        raise HarnessError(msg)
    render(manifests, Path(args.out), Path(args.workbook) if args.workbook else None)
    print(f"wrote {args.out}: {len(manifests)} run(s)")
    return 0


def _manifests(runs_dir: Path) -> list[tuple[Path, Manifest]]:
    return [(p, load_manifest(p)) for p in sorted(runs_dir.rglob("manifest.json"))]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description=__doc__)
    parser.add_argument("--config", default="harness.toml", help="path to harness.toml (default: ./harness.toml; the local demo is demo/harness.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="pending revert marker and recent runs")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("dump-workbook", help="print a BA workbook as text, one line per row, for the compile skill")
    p.add_argument("workbook")
    p.set_defaults(func=cmd_dump_workbook)

    p = sub.add_parser("validate", help="check specs against the schema, the [[allow]] list and the operations")
    p.add_argument("spec", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("sql", help="run one read-only SELECT against QA (discovery)")
    p.add_argument("sql")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("run-case", help="show the plan; with --yes set up, run, collect, and revert")
    p.add_argument("spec")
    p.add_argument("--yes", action="store_true", help="execute. Never pass this before the user has approved the plan")
    p.set_defaults(func=cmd_run_case)

    p = sub.add_parser("revert", help="undo a run's remaining setup steps (after a crash or failed revert). Target: run folder or manifest.json")
    p.add_argument("manifest")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_revert)

    p = sub.add_parser("results", help="summarize a run's evidence, or run duckdb SQL over it (--sql). Target: run folder, manifest.json, other dir or file")
    p.add_argument("target")
    p.add_argument("--sql", help="duckdb SQL; each parquet/csv/json file is a view named by its file stem")
    p.set_defaults(func=cmd_results)

    p = sub.add_parser("verdict", help="record pass/fail/inconclusive with evidence into a run's manifest")
    p.add_argument("manifest")
    p.add_argument("result", choices=["pass", "fail", "inconclusive"])
    p.add_argument("--evidence", action="append", default=[], help="one quoted observation per flag; repeatable")
    p.add_argument("--notes", default="")
    p.set_defaults(func=cmd_verdict)

    p = sub.add_parser("report", help="render every manifest into Results/Runs/Changes sheets of an Excel file")
    p.add_argument("--out", required=True)
    p.add_argument("--workbook", help="the BA workbook to copy and append the sheets to")
    p.add_argument("--runs", help="runs directory (default: paths.runs from harness.toml)")
    p.set_defaults(func=cmd_report)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
