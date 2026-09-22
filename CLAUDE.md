# Web app test harness template

This repo is a Copier template. The harness project lives in `template/`; `copier.yml` holds the questions and the
app-owned file list.

- Develop inside `template/` like any uv project: `uv run pytest`, `uv run pre-commit run --all-files`.
- `./check.sh` stamps a copy with default answers and runs its tests and pre-commit. Run it before finishing a change.
- Only `README.md.jinja` and `CLAUDE.md.jinja` are templated (`{{ app_name }}`). Keep `{{`/`{%` out of other files,
  or give them a `.jinja` suffix.
- Tests must use only template-owned files (`harness/`, `demo/`), never `harness.toml`, `specs/` or app docs, so they
  pass in stamped copies.
- A new app-owned file goes in `_skip_if_exists` in `copier.yml`, or `copier update` will overwrite apps' versions.
