# Optimizer test harness

An LLM-driven harness for testing a portfolio optimizer against BA-written test cases. Claude interprets the English,
picks portfolios, and judges results. A small CLI does everything with side effects: allowlisted single-row data
changes in the QA database with a before-image ledger, submitting and polling the optimizer's REST API, reading the
parquet output, reverting, and rendering an Excel report BAs can open.

```text
workbook.xlsx ──compile skill──▶ specs/*.yaml ──run skill + CLI──▶ runs/<case>/<ts>.json ──report──▶ results.xlsx
                (Claude reads,        (reviewed by                 (manifest: changes, run id,        (Results, Runs,
                 queries, writes)      you and BAs)                 url, status, verdict)              Changes sheets)
```

## Quick start

```bash
uv sync
cp .env.example .env
uv run python -m harness.stub --init-db local.sqlite      # terminal 1: fake optimizer + sample database
uv run --env-file .env harness run-case specs/EXAMPLE-001.yaml --yes
uv run --env-file .env harness report --out results.xlsx --workbook examples/ba-test-cases.xlsx
```

`docs/setup.md` has the full local walkthrough and the QA configuration steps.

## Layout

| Path | What |
|---|---|
| `harness/cli.py` | the commands: `status`, `dump-workbook`, `validate`, `sql`, `run-case`, `revert`, `results`, `verdict`, `report` |
| `harness/db.py` | connection adapter (sqlite, Oracle, MSSQL), allowlisted updates, ledger, revert, read-only query |
| `harness/run.py` | the case lifecycle and the manifest; revert lives in a `finally` |
| `harness/optimizer.py` | submit and poll |
| `harness/results.py` | parquet summaries and duckdb SQL |
| `harness/report.py` | workbook dump and the Excel report |
| `harness/stub.py` | fake optimizer API and sample database for local runs and tests |
| `harness.toml` | dialect, QA guard, `[[allow]]` list, API field names, paths |
| `specs/` | compiled test cases, one YAML per case |
| `.claude/skills/` | `optimizer-test-compile` and `optimizer-test-run` |
| `docs/` | glossary, schema, API and parquet notes to fill in at work; setup |
| `examples/ba-test-cases.xlsx` | a sample BA workbook to practice on |

## Safety properties, enforced in the CLI

- Refuses any DSN that does not look like QA (`qa_markers`).
- Changes touch only allowlisted tables and columns, one row each, identified by the full key. Zero or several rows is a rollback.
- Each case's changes commit together; the revert is one transaction too, and always runs.
- A failed revert leaves `runs/PENDING_REVERT`. Every command then prints a banner and `run-case` refuses to start.
- Mutating commands show their plan and do nothing without `--yes`.

## Status

Built ahead of access to the real systems. Fill in `docs/` and `harness.toml` with the real schema, API and allowlist,
then run against QA. Open items are listed at the end of `docs/setup.md`.
