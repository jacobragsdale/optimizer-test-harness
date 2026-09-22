#!/usr/bin/env bash
# Stamp the template with default answers into a temp dir and run the stamped project's own checks there.
# Uncommitted template changes are included (copier warns about a dirty template; that is expected here).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
uvx copier copy --defaults --quiet --vcs-ref HEAD "$here" "$out"  # default is the latest tag
cd "$out"
git init -q && git add -A
uv sync --quiet
uv run pytest -q
uv run pre-commit run --all-files
echo "check.sh: stamped copy passes its tests and pre-commit"
