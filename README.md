# Web app test harness template

A [Copier](https://copier.readthedocs.io) template for an LLM-driven test harness: Claude compiles BA-written test
cases from an Excel workbook into specs, runs them against a web app's QA environment through a CLI that sets up
data, calls the app's API, and reverts everything afterwards, then reports back to BAs in Excel.

## Stamp a harness for an app

```bash
uvx copier copy <this repo's git URL or path> <app>-test-harness
cd <app>-test-harness && git init && git add -A && git commit -m "Stamp test harness"
```

Then open Claude Code in the new repo and ask it to set the harness up for the app: the `harness-setup` skill asks
for the QA URL, auth, API docs and DSNs, maps the schema read-only, and writes `harness.toml`, `.env` and `docs/`.
Until then the copy runs the bundled demo app (`docs/setup.md` in the copy).

## Update a stamped harness

```bash
uvx copier update      # in the stamped repo, with a clean working tree
```

Template-owned files (`harness/`, `tests/`, `demo/`, `.claude/skills/`, `docs/setup.md`, README, CLAUDE.md) are
updated. App-owned files (`harness.toml`, `.env`, `specs/`, `docs/glossary.md`, `docs/schema.md`, `docs/api.md`,
`docs/outputs.md`) are left alone. Stamped repos should only ever change app-owned files; a fix to anything else
belongs here, so every app gets it.

## Work on the template

The project lives in `template/`, a normal uv project; its tests use the demo app and `demo/` only, so they pass
unchanged in every stamped copy.

```bash
cd template && uv sync && uv run pytest && uv run pre-commit run --all-files   # while developing
./check.sh                                                                     # stamps a copy and runs its checks
```

Tag a release (`git tag v0.2.0`) after merging; `copier update` moves stamped repos to the latest tag.
