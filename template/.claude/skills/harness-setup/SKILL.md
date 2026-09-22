---
name: harness-setup
description: "Point this test harness at a web app: config, DB allowlist, API operations. Use when the user wants to set up, configure or adapt the harness for an app, even if they don't say setup. Not for writing test specs."
---

# Harness setup

Adapt this copy of the harness to one web app by writing only app-owned files: `harness.toml`, `.env`,
`docs/glossary.md`, `docs/schema.md`, `docs/api.md` and `docs/outputs.md`. Never edit `harness/`, `tests/`, `demo/` or
`.claude/skills/`: they belong to the template and `copier update` replaces them. When the harness itself cannot do
what the app needs, stop and tell the user; do not patch the code.

## Workflow

Every command is `uv run --env-file .env harness ...` from the repo root.

1. Collect what the repo and the user's documents do not already say. Ask one to three questions at a time:
   - QA base URL, and how the API authenticates: a login call that sets a cookie, a bearer token, or an API key header.
   - The API docs. When an OpenAPI spec exists, read it instead of asking about endpoints.
   - The calls that run the app for one test: submit, poll, fetch results. For the poll: the status field, the
     terminal values, and which of those mean success.
   - DB dialect, the QA read-write DSN, and a SELECT-only DSN.
   - Where results land: response bodies, files on a share, or tables the app writes.
   - A sample BA workbook.
2. Write `.env` from `.env.example`: both DSNs plus every secret, named `HARNESS_*`. Run `uv sync --extra oracle` or
   `--extra mssql`. Do not continue to step 3 without `HARNESS_DB_RO_DSN`; discovery on the read-write account is
   what the read-only DSN exists to prevent.
3. Write a minimal `harness.toml` (`[db]`, `[api] base_url`, `[paths]`, copied in shape from `demo/harness.toml`) so
   `harness sql` runs. Then map the tables the workbook's cases touch, read-only:
   - Oracle: `ALL_TAB_COLUMNS`, `ALL_CONSTRAINTS` (`CONSTRAINT_TYPE = 'R'`, `DELETE_RULE`), `ALL_TRIGGERS`.
   - MSSQL: `INFORMATION_SCHEMA.COLUMNS`, `sys.foreign_keys` (`delete_referential_action_desc`), `sys.triggers`,
     `sys.identity_columns`.
   - sqlite: `SELECT * FROM pragma_table_info('T')`, `SELECT * FROM pragma_foreign_key_list('T')`.
4. Propose the `[[allow]]` list and get the user's yes for every table and op before writing it. Never widen the
   allowlist on your own, not even to make a case runnable. Rules for each table:
   - `key` is the primary key, or a unique key, exactly.
   - `delete` only when no foreign key into the table cascades or sets null, no delete trigger writes elsewhere, and
     (MSSQL) the table has no identity column. A revert re-inserts the saved row, so rows lost to a cascade would
     never come back.
   - `insert` only when the test can supply the whole key, so no sequence or identity key.
   - Never allow audit, history or log tables, or tables the app writes its results to.
   - Record every exclusion and its reason under "Allowed changes" in `docs/schema.md`.
5. Write one `[[operation]]` per call: login (then set `api.login`), each run call, each setup call.
   - Any operation a spec may use as setup needs an `undo` whose path and body use only variables that the setup
     operation captures. An API change without an undo cannot be set up by the harness; say so to the user.
   - Polled calls get `poll`. `capture` every id a later step needs, by dot path into the JSON response.
   - Secrets appear only as `${HARNESS_...}`. A literal secret in `harness.toml` ends up in git.
6. Set `[db] qa_markers` to substrings that both the QA DSN and the base URL contain (for example `["qa"]`). Never leave
   it empty for a real app. Set `[outputs] paths` and `[report] link` from what step 1 found.
7. Fill in the four docs, replacing the demo rows:
   - `glossary.md`: every BA phrase in the workbook, mapped to a table and column, an operation, or an output column.
   - `schema.md`: discovery queries with their joins, allowed changes, and the step 4 exclusions.
   - `api.md`: the operations, their statuses, and auth.
   - `outputs.md`: each evidence file or view, its grain, key columns and units.
8. Smoke test. Compile one real case with the test-compile skill, run `harness validate`, then run `harness run-case`
   without `--yes` and show the user the plan. Hand off to the test-run skill only after the user says go.

## Validation

- `harness status` exits 0. A config problem prints `error:` and exits 2; fix `harness.toml` and rerun.
- `harness sql "SELECT 1 ..."` prints no read-write warning.
- The step 8 plan shows a current DB value for every change, and full URLs for every call.
- After the first real run: the output ends `reverted: True`, and `harness status` reports no pending revert.
- `git status` shows changes only to app-owned files.

## Example

The workbook asks to "turn off the fraud rule" on a claim type. Discovery finds a cascade:

```text
harness sql "SELECT table_name, delete_rule FROM all_constraints WHERE constraint_type = 'R'
             AND r_constraint_name IN (SELECT constraint_name FROM all_constraints
                                       WHERE table_name = 'CLAIM_RULE' AND constraint_type = 'P')"
-> CLAIM_RULE_AUDIT | CASCADE
```

Proposal to the user: "CLAIM_RULE: key RULE_ID; update ENABLED and THRESHOLD; insert yes; delete no, because
CLAIM_RULE_AUDIT cascades. OK?" After the yes, `harness.toml` gets:

```toml
[[allow]]
table = "CLAIM_RULE"
key = ["RULE_ID"]
columns = ["ENABLED", "THRESHOLD"]
ops = ["update", "insert"]
```

`docs/schema.md` records: "CLAIM_RULE: delete not allowed; CLAIM_RULE_AUDIT has ON DELETE CASCADE."

## Bundled resources

All paths are relative to the repo root.

- `demo/harness.toml`: read as the complete worked config (login, API setup with undo, polling, outputs, link).
- `docs/setup.md`: read for the QA configuration steps and what the CLI enforces.
