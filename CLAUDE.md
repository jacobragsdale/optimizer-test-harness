# Optimizer test harness

Claude drives BA-written optimizer test cases: compile the workbook into specs, run each spec through the CLI, judge
the parquet output, report to Excel. Two skills carry the procedure; the CLI carries every side effect.

- Run everything as `uv run --env-file .env harness <command>`; `harness --help` lists the commands.
- Never run SQL that changes data and never call the optimizer API outside the CLI.
- `--yes` executes `run-case` and `revert`. Do not pass it until the user has seen the plan and said go.
- A `PENDING_REVERT` banner means changes are sitting in QA. Fix that first, always.
- Skills: `.claude/skills/optimizer-test-compile` (workbook to specs), `.claude/skills/optimizer-test-run` (spec to verdict and report).
- Domain reference to fill in at work: `docs/glossary.md`, `docs/schema.md`, `docs/api.md`, `docs/parquet.md`.
- Local dry run and QA setup: `docs/setup.md`. Checks: `uv run pytest`, `uv run pre-commit run --all-files`.
