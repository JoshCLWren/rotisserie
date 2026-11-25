#!/usr/bin/env bash
# Runs inside the scripts/test.Dockerfile image. Each project gets its own
# Linux virtualenv so the host's macOS .venv directories are never used.
set -euo pipefail

cd /work

in_project() {
  local project="$1"
  shift
  local dir=/work
  [[ "$project" == root ]] || dir="/work/$project"
  (cd "$dir" && UV_PROJECT_ENVIRONMENT="$HOME/venvs/$project" "$@")
}

full_ci() {
  in_project root uv sync --locked --python 3.14
  in_project root uv run --locked --python 3.14 ./scripts/verify
  in_project factory uv sync --locked --python 3.14
  in_project factory uv run --locked --python 3.14 pytest -q -n auto
  in_project factory bash -c 'node --test .github/scripts/*.test.cjs scripts/local-factory-acp-review.test.mjs'
  in_project factory bash -c "find .github/scripts scripts -type f -name '*.sh' -print0 | xargs -0 -r -n 1 bash -n"
  in_project factory bash -c "find .github/scripts scripts -type f -name '*.sh' -print0 | xargs -0 -r shellcheck --severity=error"
}

case "${1:-}" in
  "") full_ci ;;
  root | factory)
    project="$1"
    shift
    [[ $# -gt 0 ]] || { echo "usage: test-in-docker $project <command...>" >&2; exit 2; }
    in_project "$project" uv sync --locked --python 3.14 --quiet
    in_project "$project" "$@"
    ;;
  *) echo "usage: test-in-docker [root|factory <command...>]" >&2; exit 2 ;;
esac
