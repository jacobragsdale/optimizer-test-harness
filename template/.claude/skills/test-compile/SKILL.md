---
name: test-compile
description: "Compile BA test cases from an Excel workbook into YAML specs. Use whenever the user wants to read, interpret, or translate test cases from a workbook, pick test data for a case, or write specs, even if they don't say compile."
---

# Test compile

Turn each BA-written test case in an Excel workbook into one reviewable YAML spec under `specs/`. This skill never
changes QA data or calls the app; its only database access is read-only SQL for discovery.

## Asking

Ask when the answer changes a setup step, the subject, the evaluation plan or which cases compile, or when two
readings are both plausible. Otherwise take the default the glossary, the docs or an earlier answer gives, and say
which one you took.

- Collect the open questions first, then ask up to four per round. Give a choice two to four concrete options,
  your recommendation first with its reason; ask for a plain fact (a URL, a path) plainly.
- Wait for the answers; never go on with an answer you assumed. Ask more rounds until nothing blocking remains.
- Record glossary answers in `docs/glossary.md` and case answers in the spec's `interpretation`, so no later session
  asks them again.

## Workflow

Every command is `uv run --env-file .env harness ...` from the repo root. Run it, read the output, decide.

1. Read `docs/glossary.md`, `docs/schema.md`, `docs/api.md`, `docs/outputs.md`, and the `[[allow]]` and
   `[[operation]]` entries in `harness.toml`.
2. Run `harness dump-workbook <workbook.xlsx>`. Every non-empty row prints with its sheet and row number; that pair is
   the spec's `source`. List the test case rows you found and any `specs/<case_id>.yaml` that already exists (it may
   carry BA corrections). Ask which cases to compile unless the request names them, and whether to overwrite
   existing specs; step 3 depends on both answers.
3. For each case, work out a reading and find the subject (the account, product, order... the case is about) with
   `harness sql "<SELECT ...>"`. Query freely; discovery is read-only. Prefer a subject whose current data already
   satisfies most of the setup, so the setup stays short. Write no spec yet.
4. Clarify, in rounds covering all cases, before writing any spec:
   - Rows with two plausible readings: each reading and what it changes in the setup or the evaluation.
   - BA phrases missing from the glossary: the entry you propose.
   - Expectations with no checkable value ("prices update correctly"): the value, or the tolerance to compare with.
   - Subjects whose candidates could give different outcomes (for example, one also has a second active discount).
   - Cases that need a table, column, op or call outside `harness.toml`: skip the case and mark it blocked, propose
     the change through the harness-setup skill, or use another reading that fits.
   When the user says to use your judgment, pick, and put both readings and your pick in `interpretation`.
5. Write `specs/<case_id>.yaml` from the template below. Copy `description` and `expected.text` verbatim. Write
   `interpretation` as one or two sentences a BA can confirm or correct without reading anything else, plus any
   step 4 answer that shaped it. Record the subject's ids and a one-line `rationale`.
6. Write `setup` only for what the test needs:
   - `db: update`: one row by its full key; `set` only the columns that must change.
   - `db: insert`: a key that is plainly test data (`R-TEST-<case>`), checked unused with `harness sql`.
   - `db: delete`: only when the test needs the row gone. The whole row is saved and put back afterwards.
   - `api: <operation>`: only operations with an `undo` in `harness.toml`.
   Only allowlisted tables, ops and columns, and defined operations. Never work around a missing permission, for
   example with an API call that changes what the allowlist forbids; step 4 settles those cases.
7. Write `run` as the operations that exercise the app, in order. Pass ids captured by earlier steps as `"{name}"`.
   Write `collect` for database evidence: rows the app writes, or setup values you need to prove were in effect.
   A collect query runs after the run and before the revert; put `{name}` in it unquoted, it is bound as a parameter.
8. Write `expected.evaluation_plan` before anything runs: which view, which columns or aggregate, and what value
   shows the expectation held. Round float sums (`round(sum(x), 6)`) so noise is never read as a breach. When the
   BA expects the app to reject the case, set `outcome: failure` and name the rejection: the final status, HTTP
   status or error text.
9. Run `harness validate specs/*.yaml` and fix every problem it prints.
10. Present the specs for review before any of them run: per case, the interpretation, subject, setup, run and
    evaluation plan. The user or a BA corrects them here, which is far cheaper than after a run.

## Spec template

Every top-level field is REQUIRED; `setup` and `collect` may be `[]`. DB `key` must be exactly the allowlisted key.

```yaml
case_id: TC-001                 # from the workbook's ID column
title: <short name>
source: {sheet: <sheet name>, row: <row number from dump-workbook>}
description: |
  <the BA's text, verbatim>
interpretation: |
  <what you understood, in terms of tables, values, operations and output columns>
subject:
  criteria: <the selection criteria in words>
  ids: [<id>]                   # each id must appear in setup, run or collect
  rationale: <why these>
setup:
  - {db: update, table: <table>, key: {<key col>: <value>}, set: {<col>: <value>}}
  - {db: insert, table: <table>, values: {<key col>: <value>, <col>: <value>}}
  - {db: delete, table: <table>, key: {<key col>: <value>}}
  - {api: <operation with an undo>, body: {<field>: <value>}}
run:
  - {api: <operation>, body: {<field>: "{<captured name>}"}}
collect:
  - {name: <view name>, sql: "SELECT ... WHERE <col> = {<captured name>}"}
expected:
  outcome: success | failure
  text: <the BA's expected result, verbatim>
  evaluation_plan:
    - "<view>: <SQL or check>; <what it must show>"
```

## Rules

- `case_id` comes from the workbook's ID column. When there is none, use `<sheet>-r<row>` and tell the user to have
  BAs add an ID column; row numbers move.
- A case pasted into the chat has no `source`. Ask for its workbook, sheet and row; `source` is required and ties the
  spec back to the BA's row.
- Values are typed by YAML: `0.10` is a number, `"0.10"` is text. Match the column. A body string that is exactly
  `"{name}"` keeps the captured value's type.
- Views for the evaluation plan: each response is named by its operation (`get_prices`; a second call
  `get_prices_2`), each collect by its `name`, each output file by its stem. Arrays inside a JSON response need
  `unnest(...)`.
- `harness sql` refuses anything but a single SELECT. Do not try to get around it; changes go through specs.

## Example

Workbook row (sheet `Pricing`, row 2):

```text
| r2 | TC-001 | Discounts | Raise the discount on the office chair to 30% and reprice it. | chair discount = 30% | The chair should come out at 175.00. |  |
```

Discovery:

```sql
SELECT p.PRODUCT_ID, p.BASE_PRICE, r.RULE_ID, r.PERCENT, r.ENABLED
FROM PRODUCT p LEFT JOIN DISCOUNT_RULE r ON r.PRODUCT_ID = p.PRODUCT_ID
WHERE p.NAME = 'Office Chair'
```

Output: `specs/TC-001.yaml`, identical in shape to `demo/specs/EXAMPLE-001.yaml`, with `interpretation` reading
"Set DISCOUNT_RULE R-2 PERCENT from 15 to 30, run a pricing job for P-200, and check that get_prices shows
final_price 175.00 (250.00 less 30%)."

## Bundled resources

All paths are relative to the repo root.

- `docs/glossary.md`, `docs/schema.md`, `docs/api.md`, `docs/outputs.md`: read before compiling; extend the glossary as you go.
- `demo/specs/EXAMPLE-00*.yaml`: read as complete, valid specs: 001 update plus collect, 002 insert, 003 delete,
  004 API setup and an output file, 005 an expected rejection.
- `harness.toml`: read `[[allow]]` for what a test may change and `[[operation]]` for what it may call.
