# Setup

## Local demo (no QA, no real app)

`harness/stub.py` is a small pricing app over a sample sqlite database, and `demo/` holds its config, five specs and
a BA workbook. It exercises everything: a login cookie, DB update, insert and delete, API setup with an undo, a
polled job, an expected rejection, and a file the app writes.

```bash
uv sync
cp .env.example .env                                                  # already points at the demo
uv run python -m harness.stub --init-db demo.sqlite                   # terminal 1: demo app on :8765, fresh sample data
uv run --env-file .env harness --config demo/harness.toml run-case demo/specs/EXAMPLE-001.yaml         # plan only
uv run --env-file .env harness --config demo/harness.toml run-case demo/specs/EXAMPLE-001.yaml --yes   # set up, run, collect, revert
uv run --env-file .env harness --config demo/harness.toml results runs/EXAMPLE-001/<ts> --sql "SELECT p.product_id, p.final_price FROM (SELECT unnest(prices) AS p FROM get_prices)"
uv run --env-file .env harness --config demo/harness.toml verdict runs/EXAMPLE-001/<ts> pass --evidence "P-200 final_price 175.0, expected 175.00" --notes "example"
uv run --env-file .env harness --config demo/harness.toml report --out results.xlsx --workbook demo/ba-test-cases.xlsx
```

To try the skills on the demo, `cp demo/harness.toml harness.toml`, open Claude Code in the repo, and ask it to
compile `demo/ba-test-cases.xlsx` or run a spec. Delete that `harness.toml` before setting up a real app.

## A real app

Open Claude Code in the repo and ask it to set the harness up for the app; the harness-setup skill takes over. It
asks for the QA URL, auth, API docs, DSNs and a sample workbook; maps the schema read-only; proposes the allowlist
for your approval; and writes `harness.toml`, `.env` and `docs/`. The same steps by hand:

1. `uv sync --extra oracle` (or `--extra mssql`). Oracle uses python-oracledb thin mode; no client install.
2. Put the DSNs and secrets in `.env` (see `.env.example`). Ask for a SELECT-only account for `HARNESS_DB_RO_DSN`; it
   is what makes discovery safe to hand to the model.
3. Write `harness.toml`, using `demo/harness.toml` as the worked example: `[db]` with `qa_markers`, the `[[allow]]`
   list, one `[[operation]]` per API call (setup operations need an `undo`), `[outputs]`, `[report]`.
4. Fill in `docs/glossary.md`, `docs/schema.md`, `docs/api.md` and `docs/outputs.md`.

Ownership: `harness/`, `tests/`, `demo/`, `docs/setup.md` and `.claude/skills/` come from the template, and
`copier update` replaces them. `harness.toml`, `.env`, `specs/` and the other docs belong to this app; edit only
those.

## What the CLI enforces, regardless of who drives it

- The DB DSN and `api.base_url` must each contain a `qa_markers` entry, or the CLI refuses.
- DB writes hit only `[[allow]]` tables, ops and columns, one row each, identified by the full key. An update or
  delete that finds no row, or an insert whose key exists, is a refusal and a rollback.
- API calls are only the operations `harness.toml` names; specs cannot carry a method or path. An operation used as
  setup must have an undo.
- Consecutive DB steps commit together, and their before-images are on disk before the commit. An API setup step is
  recorded before the call, so an interrupted call is visible.
- `run-case --yes` reverts in a `finally` block, in reverse order: DB steps by before-image (one transaction per
  group), API steps by their undo. If anything fails to revert, `runs/PENDING_REVERT` stays behind, every later
  command prints a banner, and `run-case` refuses to start until `harness revert <run> --yes` succeeds. The marker is
  created atomically, so it is also the lock that keeps two runs from overlapping.
- `harness sql` and `collect` run one SELECT/WITH with no write keywords, then roll back. The read-only account is
  the real guard.
- `${ENV}` secrets are expanded on the wire only; manifests keep the template.
- Mutating commands print their plan and do nothing without `--yes`.

## Not supported

- Cleaning up rows the app itself writes during a run. `collect` snapshots them; they stay in QA.
- Deletes on tables with cascading foreign keys or delete triggers, and inserts that need a sequence or identity key.
  Keep those tables or ops out of `[[allow]]`; harness-setup records why in `docs/schema.md`.
- An API setup call interrupted mid-flight (the process was killed): `harness revert` reports it but cannot know
  whether it took effect. Check the app, clean up by hand, then delete `runs/PENDING_REVERT`.
- `{name}` variables in DB setup values. DB setup is literal; variables work in API steps, `collect` and the link.

## Updating the harness

`uvx copier update` pulls template changes (fixes to the CLI, skills and demo) into this copy and keeps the
app-owned files.
