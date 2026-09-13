# Setup

## Local dry run (no QA, no optimizer)

```bash
uv sync
cp .env.example .env                                   # HARNESS_DB_DSN=local.sqlite is already right
uv run python -m harness.stub --init-db local.sqlite   # terminal 1: fake optimizer on :8765, sample database
uv run --env-file .env harness run-case specs/EXAMPLE-001.yaml         # plan only
uv run --env-file .env harness run-case specs/EXAMPLE-001.yaml --yes   # apply, run, poll, revert
uv run --env-file .env harness results runs/EXAMPLE-001/<ts>.json --sql "SELECT sector, sum(weight) w FROM holdings GROUP BY 1 ORDER BY 2 DESC"
uv run --env-file .env harness verdict runs/EXAMPLE-001/<ts>.json pass --evidence "max sector weight 0.20" --notes "example"
uv run --env-file .env harness report --out results.xlsx --workbook examples/ba-test-cases.xlsx
```

Then open Claude Code in the repo and ask it to compile `examples/ba-test-cases.xlsx` or run a spec; the skills take over.

## QA

1. `uv sync --extra oracle` (or `--extra mssql`). Oracle uses python-oracledb thin mode; no client install.
2. Copy `harness.toml` to `harness.qa.toml`. Set `dialect`, `qa_markers = ["qa"]` (a substring every QA DSN
   contains), the `[[allow]]` list from `docs/schema.md`, and the `[api]` block from `docs/api.md`.
3. Put the DSNs in `.env`. Ask for a SELECT-only account for `HARNESS_DB_RO_DSN`; it is what makes portfolio
   discovery safe to hand to the model.
4. Run with `uv run --env-file .env harness --config harness.qa.toml ...`.

## What the CLI enforces, regardless of who drives it

- A DSN that does not contain a `qa_markers` entry is refused.
- Changes hit only `[[allow]]` tables and columns, one row each, identified by the full key. Zero or several rows is a refusal and a rollback.
- All of a case's changes commit together or not at all. The revert is one transaction too.
- `run-case --yes` reverts in a `finally` block. If the revert itself fails, `runs/PENDING_REVERT` stays behind, every
  later command prints a banner, and `run-case` refuses to start until `harness revert <manifest> --yes` succeeds.
- Mutating commands print their plan and do nothing without `--yes`.

## Not yet supported

- Inserting or deleting rows as test setup. Only column updates on existing rows.
- Cleaning up rows the optimizer itself writes to the database, if it does.
- Oracle DATE columns in the allowlist: the revert binds the before-image as text, which needs `TO_DATE`.
