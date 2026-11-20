"""End to end: a model edit survives checkpointing, adoption and the scope guard.

Runs the real checkpoint writer, the sourced worker primitives and the real
fixed-model guard against a local bare remote. Only GitHub is faked.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"

FAKE_GH = r'''#!/usr/bin/env bash
set -euo pipefail
jqexpr=''
args=()
while (( $# )); do
  case "$1" in
    --jq|-q) jqexpr="$2"; shift 2 ;;
    --json) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
case "${args[0]} ${args[1]:-}" in
  "pr view") out='{"title":"Fix the widget","body":"Closes #7","headRefName":"feature"}' ;;
  "issue view") out='{"title":"Widget is wrong","body":"Change app.py so the widget value is correct."}' ;;
  *)
    endpoint="${args[1]}"
    if [[ "$endpoint" == */compare/* ]]; then
      range="${endpoint##*/compare/}"
      out="$(git diff --name-only "${range%%...*}" "${range##*...}" | jq -R -s '{files: (split("\n") | map(select(length>0)) | map({filename: .}))}')"
    elif [[ "$endpoint" == */pulls/* ]]; then
      out="$(jq -nc --arg sha "$(git rev-parse origin/main)" '{base:{sha:$sha}}')"
    else
      out='{}'
    fi ;;
esac
if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" <<< "$out"; else printf '%s\n' "$out"; fi
'''

SCENARIO = r'''
set -Eeuo pipefail
export FACTORY_WORKER=42 FACTORY_SOURCE=nvidia FACTORY_MODEL=test/model FACTORY_RUNTIME_MODEL=nvidia/test/model
export GITHUB_REPOSITORY=test/repo RUNNER_TEMP="$TMP" TRUSTED_GUARD="$TMP/guard.py"
export TRUSTED_REVIEW_CONTROLLER="$SCRIPTS/factory-work-controller.py"
work="$PWD"; cd "$SCRIPTS/../.."
source "$SCRIPTS/free-model-factory-worker-primitives.sh"
cd "$work"
set +e +u
MODE=pr NUMBER=5
EXPECTED_HEAD="$(git rev-parse HEAD)"
CHECKPOINT_REF_FILE="$TMP/ref"
export FACTORY_CHECKPOINT_REF_FILE="$CHECKPOINT_REF_FILE" FACTORY_CHECKPOINT_INTERVAL_SECONDS=1 TRUSTED_CHECKPOINT_HANDOFF=''
bash "$SCRIPTS/factory-work-checkpoint.sh" pr 5 42 123 > "$TMP/checkpoint.log" 2>&1 &
pid=$!

# The fake agent starts after the writer records its base, as a real model does.
sleep 1
printf 'widget = 2\nother = 0\n' > app.py
if [[ "$AGENT" == commits ]]; then git commit -qam "model commit"; fi
sleep 4

kill -USR1 "$pid"; wait "$pid" || true
[[ -s "$CHECKPOINT_REF_FILE" ]] || { echo "NO_CHECKPOINT"; cat "$TMP/checkpoint.log"; exit 3; }
adopt_persisted_work || { echo "ADOPT_FAILED"; exit 4; }
if reject_out_of_scope_diff pr 5 "$EXPECTED_HEAD"; then echo "GUARD=accepted"; else echo "GUARD=rejected"; fi
echo "APP=$(head -1 app.py)"
'''


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


def run(tmp_path: Path, agent: str) -> str:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
        git(work, "config", key, value)
    (work / "app.py").write_text("widget = 1\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    git(work, "push", "-q", "origin", "main")
    git(work, "switch", "-qc", "feature")
    (work / "app.py").write_text("widget = 1\nother = 0\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "pr head")
    git(work, "push", "-q", "origin", "feature")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TMP": str(tmp_path),
           "SCRIPTS": str(SCRIPTS), "AGENT": agent}
    result = subprocess.run(["bash", "-c", SCENARIO], cwd=work, env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_checkpointed_model_edit_reaches_push(tmp_path: Path) -> None:
    out = run(tmp_path, "edits")
    assert "GUARD=accepted" in out, out
    assert "APP=widget = 2" in out


def test_model_that_commits_itself_is_still_rejected(tmp_path: Path) -> None:
    out = run(tmp_path, "commits")
    assert "GUARD=rejected" in out, out
