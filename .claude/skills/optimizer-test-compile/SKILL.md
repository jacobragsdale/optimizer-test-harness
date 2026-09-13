---
name: optimizer-test-compile
description: "Compile BA test cases from an Excel workbook into YAML specs. Use whenever the user wants to read, interpret, or translate test cases from a workbook, pick portfolios for a test, or write specs, even if they don't say compile."
---

# Optimizer test compile

Turn each BA-written test case in an Excel workbook into one reviewable YAML spec under `specs/`. This skill never
touches QA data or the optimizer; the only database access is read-only SQL for finding portfolios.

## Workflow

Every command is `uv run --env-file .env harness ...` from the repo root. Run it, read the output, decide.

1. Read `docs/glossary.md`, `docs/schema.md` and `docs/api.md`. They map BA vocabulary to tables, columns, request
   fields and output columns. When a BA phrase is missing, add your reading to the glossary table and flag it in the
   spec's `interpretation`.
2. Run `harness dump-workbook <workbook.xlsx>`. Every non-empty row prints with its sheet and row number; that pair is
   the spec's `source`.
3. For each test case row, write `specs/<case_id>.yaml` using the template below. Copy `description` verbatim.
   Write `interpretation` as one or two sentences a BA can confirm or correct without reading anything else.
4. Find portfolios with `harness sql "<SELECT ...>"`. Query freely; discovery is read-only. Record the chosen ids
   and a one-line `rationale`. Prefer a portfolio whose current data already satisfies most of the setup, so the
   change list stays short.
5. Write `changes` only for setup the test actually needs: one row, one column, one value each. Only tables and
   columns in the `[[allow]]` list of `harness.toml` are permitted. When a test needs anything else, stop and tell
   the user which table and column; do not work around it.
6. Write `expected.evaluation_plan` before anything runs: which parquet file, which columns or aggregate, and what
   value or relationship shows the expectation held. When the BA expects the optimizer to reject the run, set
   `outcome: failure` and describe the rejection instead.
7. Run `harness validate specs/*.yaml` and fix every problem it prints.
8. Present the specs for review before any of them run: per case, the interpretation, portfolio, changes, and
   evaluation plan. The user or a BA corrects them here, which is far cheaper than after a run.

## Spec template

Every field is REQUIRED. `key` must be exactly the allowlisted key columns for the table.

```yaml
case_id: TC-001                 # from the workbook's ID column
title: <short name>
source: {sheet: <sheet name>, row: <row number from dump-workbook>}
description: |
  <the BA's text, verbatim>
interpretation: |
  <what you understood, in terms of tables, values and output columns>
portfolio:
  criteria: <the selection criteria in words>
  ids: [<portfolio id>]
  rationale: <why this one>
changes:
  - table: <allowlisted table>
    key: {<key col>: <value>, <key col>: <value>}
    column: <allowlisted column>
    value: <new value>
run:
  params: {<request body, sent verbatim>}
expected:
  outcome: success | failure
  text: <the BA's expected result, verbatim>
  evaluation_plan:
    - "<file>: <SQL or check>; <what it must show>"
```

## Rules

- `case_id` comes from the workbook's ID column. When there is none, use `<sheet>-r<row>` and tell the user to have
  BAs add an ID column; row numbers move.
- A row whose reading changes the setup or the evaluation gets both readings in `interpretation` plus the one you
  chose. Never pick silently.
- Values in `changes` are typed by YAML: `0.10` is a number, `"0.10"` is text. Match the column.
- `harness sql` refuses anything but a single SELECT. Do not try to get around it; changes go through specs.

## Example

Workbook row (sheet `New Feature`, row 4):

```text
| r4 | TC-001 | Sector cap | Take a mid cap portfolio that has a sector constraint. Lower the sector max to 10% and rerun. | sector max = 10% | No sector in the optimized holdings should be above 10%. | |
```

Discovery:

```sql
SELECT p.PORTFOLIO_ID, p.MARKET_CAP_BUCKET, c.CONSTRAINT_TYPE, c.LIMIT_VALUE, c.ENABLED
FROM PORTFOLIO p JOIN PORTFOLIO_CONSTRAINT c ON c.PORTFOLIO_ID = p.PORTFOLIO_ID
WHERE p.MARKET_CAP_BUCKET = 'MID' AND c.CONSTRAINT_TYPE = 'SECTOR_MAX' AND c.ENABLED = 1
```

Output: `specs/TC-001.yaml`, identical in shape to `specs/EXAMPLE-001.yaml`, with `interpretation` reading
"Set SECTOR_MAX LIMIT_VALUE on PF-1002 from 0.20 to 0.10, run PF-1002, and check that summed weight per sector in
holdings.parquet never exceeds 0.10."

## Bundled resources

All paths are relative to the repo root.

- `docs/glossary.md`, `docs/schema.md`, `docs/api.md`, `docs/parquet.md`: read before compiling; extend the glossary as you go.
- `specs/EXAMPLE-001.yaml`: read as a complete, valid spec against the local sample database.
- `harness.toml`: read the `[[allow]]` list to know what a test may change.
