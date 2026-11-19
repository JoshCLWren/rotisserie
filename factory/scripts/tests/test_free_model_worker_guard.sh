#!/usr/bin/env bash
set -Eeuo pipefail

# ===========================================================================
# Test suite for the fixed-model free-model-factory-worker.sh pre-push guard.
#
# Regression 1: a target PR branch that carries an OLD copy of
# fixed-model-guard.py (zero-overlap detection only) must not weaken the
# pre-push decision. The worker must execute the trusted guard staged from the
# main checkout and reject factory-control edits on a normal product task.
#
# Regression 2: a model that runs `git merge origin/main` and leaves
# MERGE_HEAD / unmerged index entries / conflict-marker text must be rejected
# and reset without commit or push.
#
# Regression 3: a model that completes a merge or rebase of main (moving HEAD
# away from the checked-out target) must be rejected.
# ===========================================================================

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORKER="$ROOT/.github/scripts/free-model-factory-worker.sh"
TRUSTED_GUARD_SRC="$ROOT/.github/scripts/fixed-model-guard.py"
PASS=0
FAIL=0
TEST_TMPDIR=""

cleanup() {
  if [[ -n "${TEST_TMPDIR:-}" && -d "$TEST_TMPDIR" ]]; then
    rm -rf "$TEST_TMPDIR"
  fi
  TEST_TMPDIR=""
}
trap cleanup EXIT

fail() {
  FAIL=$((FAIL + 1))
  printf '  FAIL: %s\n' "$*" >&2
}

pass() {
  PASS=$((PASS + 1))
  printf '  PASS: %s\n' "$*"
}

assert_eq() {
  local expected="$1" actual="$2" msg="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass "$msg"
  else
    fail "$msg — expected '$expected', got '$actual'"
  fi
}

assert_true() {
  local msg="$1"
  shift
  if "$@"; then
    pass "$msg"
  else
    fail "$msg"
  fi
}

assert_false() {
  local msg="$1"
  shift
  if "$@"; then
    fail "$msg"
  else
    pass "$msg"
  fi
}

# Extract named function definitions from the worker script.
# The declaration line is matched with grep -F (fixed string, no awk regex
# escape sequences -- gawk and mawk disagree on \( \) \{). Body extraction
# counts { } characters in awk, which needs no regex at all, so it is
# portable across awk flavors.
extract_functions() {
  local name defs="" start_line="" body=""
  for name in "$@"; do
    start_line="$(grep -n -F "${name}() {" "$WORKER" | head -1 | cut -d: -f1)"
    if [[ -z "$start_line" ]]; then
      printf 'function %s not found in %s\n' "$name" "$WORKER" >&2
      return 1
    fi
    body="$(sed -n "${start_line},\$p" "$WORKER" | awk '
      {
        print
        for (i = 1; i <= length($0); i++) {
          c = substr($0, i, 1)
          if (c == "{") depth++
          if (c == "}") depth--
        }
        if (depth <= 0) exit
      }
    ')"
    defs="$defs
$body"
  done
  eval "$defs"
}

# Minimal OLD guard implementation: zero-overlap detection only. This mirrors
# the 8af84904-era helper that did not implement factory-control scope checks,
# which is what let the contaminated PR downgrade the pre-push decision.
write_stale_guard() {
  local path="$1"
  cat > "$path" <<'EOF'
#!/usr/bin/env python3
"""Stale helper: zero-overlap detection only (pre-factory-control-check)."""

from __future__ import annotations

import argparse
import json


def unrelated_repair(previous_pr_files, latest_commit_files):
    return bool(
        previous_pr_files
        and latest_commit_files
        and previous_pr_files.isdisjoint(latest_commit_files)
    )


def reject_decision(previous_pr_files, latest_commit_files, *, title="", body=""):
    previous = set(p for p in previous_pr_files if p)
    latest = set(p for p in latest_commit_files if p)
    if unrelated_repair(previous, latest):
        return {"reject": True, "reason": "zero-overlap-with-existing-pr-diff"}
    return {"reject": False, "reason": "allowed"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-json")
    parser.add_argument("--latest-json")
    parser.add_argument("--title", default="")
    parser.add_argument("--body", default="")
    args = parser.parse_args()
    print(json.dumps(reject_decision(
        set(json.loads(args.previous_json or "[]")),
        set(json.loads(args.latest_json or "[]")),
    )))


if __name__ == "__main__":
    raise SystemExit(main())
EOF
  chmod +x "$path"
}

new_work() {
  # Fresh clone of a shared bare remote with a main branch.
  cd /
  cleanup
  TEST_TMPDIR="$(mktemp -d)"
  git init -q --bare "$TEST_TMPDIR/remote.git"
  git clone -q "$TEST_TMPDIR/remote.git" "$TEST_TMPDIR/work"
  git -C "$TEST_TMPDIR/work" config user.email "test@example.com"
  git -C "$TEST_TMPDIR/work" config user.name "Test"
  git -C "$TEST_TMPDIR/work" checkout -q -b main
  git -C "$TEST_TMPDIR/work" config push.default current
  echo "init" > "$TEST_TMPDIR/work/README.md"
  git -C "$TEST_TMPDIR/work" add -A
  git -C "$TEST_TMPDIR/work" commit -qm init
  git -C "$TEST_TMPDIR/work" push -q -u origin main
}

stage_real_guard_on_main() {
  # The trusted main checkout contains the current fixed-model-guard.py.
  cd "$TEST_TMPDIR/work"
  mkdir -p .github/scripts
  cp "$TRUSTED_GUARD_SRC" .github/scripts/fixed-model-guard.py
  git add -A && git commit -qm "add trusted guard"
  git push -q origin main
}

# ===========================================================================
# TEST: trusted guard is staged from the main checkout and self-tests.
# ===========================================================================
test_trusted_guard_staging() {
  printf '\n--- test_trusted_guard_staging ---\n'
  new_work
  stage_real_guard_on_main
  extract_functions stage_trusted_guard
  TRUSTED_GUARD="${TEST_TMPDIR}/trusted-fixed-model-guard.py"
  stage_trusted_guard
  assert_true "trusted guard copy exists in test workspace" test -f "${TRUSTED_GUARD:-}"
  assert_true "trusted copy carries the new git-state logic" \
    grep -q 'conflict_markers' "${TRUSTED_GUARD}"
}

# ===========================================================================
# REGRESSION 1: stale branch guard helper must not weaken the trusted guard.
# ===========================================================================
test_regression_stale_branch_guard() {
  printf '\n--- test_regression_stale_branch_guard ---\n'
  new_work
  stage_real_guard_on_main
  extract_functions stage_trusted_guard
  TRUSTED_GUARD="${TEST_TMPDIR}/trusted-fixed-model-guard.py"
  stage_trusted_guard

  cd "$TEST_TMPDIR/work"
  git checkout -q -b feature
  git reset -q --hard origin/main
  write_stale_guard .github/scripts/fixed-model-guard.py
  mkdir -p frontend/src/pages/RollPage/components frontend/src/components frontend/src/unit
  echo "product" > frontend/src/pages/RollPage/components/RatingView.tsx
  git add -A && git commit -qm "product work with stale branch guard helper"
  EXPECTED_HEAD="$(git rev-parse HEAD)"

  # The stale branch helper is what the old buggy worker would have executed.
  # The latest diff overlaps the prior product diff (real contamination had
  # product overlap), so the stale zero-overlap-only helper passes it.
  stale_decision="$(python3 .github/scripts/fixed-model-guard.py \
    --previous-json '["frontend/src/components/ContinuityCorrectionDialog.tsx","frontend/src/pages/RollPage/components/RatingView.tsx"]' \
    --latest-json '["frontend/src/components/ContinuityCorrectionDialog.tsx","frontend/src/unit/ContinuityCorrectionDialog.test.tsx",".github/scripts/classify-fixed-model-run.py",".github/scripts/fixed-model-guard.py"]' \
    --title "Continuity correction" \
    --body "Part of #852.")"
  assert_eq "false" \
    "$(jq -r .reject <<< "$stale_decision")" \
    "stale branch guard misses factory-control (vulnerability reproduced)"

  # The model then edits factory-control files on top of the product PR.
  mkdir -p .github/scripts
  echo "CONTAMINATED" > .github/scripts/classify-fixed-model-run.py
  echo "still product" >> frontend/src/pages/RollPage/components/RatingView.tsx

  # The trusted guard (staged from main) must reject despite the stale helper.
  trusted_decision="$(python3 "$TRUSTED_GUARD" \
    --previous-json '["frontend/src/components/ContinuityCorrectionDialog.tsx","frontend/src/pages/RollPage/components/RatingView.tsx",".github/scripts/fixed-model-guard.py"]' \
    --latest-json '["frontend/src/unit/ContinuityCorrectionDialog.test.tsx",".github/scripts/classify-fixed-model-run.py",".github/scripts/fixed-model-guard.py"]' \
    --git-state '{"merge_in_progress":false,"cherry_pick_in_progress":false,"revert_in_progress":false,"unmerged_entries":false,"conflict_markers":false,"head_changed":false,"foreign_main_commits":false}' \
    --title "Add in-context continuity correction from the Roll rating screen" \
    --body "Part of #852.")"
  assert_eq "true" \
    "$(jq -r .reject <<< "$trusted_decision")" \
    "trusted guard rejects factory-control on product task"
}

# ===========================================================================
# REGRESSION 2: model runs `git merge origin/main`; persistence must reject.
# ===========================================================================
test_regression_merge_main_rejected() {
  printf '\n--- test_regression_merge_main_rejected ---\n'
  new_work
  extract_functions stage_trusted_guard unclean_git_state_json conflict_markers_present branch_folded_main_commits

  # The worker stages the trusted guard while on the main checkout.
  cd "$TEST_TMPDIR/work"
  mkdir -p .github/scripts
  cp "$TRUSTED_GUARD_SRC" .github/scripts/fixed-model-guard.py
  git add -A && git commit -qm "add trusted guard"
  git push -q origin main
  TRUSTED_GUARD="${TEST_TMPDIR}/trusted-fixed-model-guard.py"
  stage_trusted_guard

  git checkout -q -b feature
  git reset -q --hard origin/main
  echo "conflict base" > conflicted.txt
  git add -A && git commit -qm "feature adds conflicted.txt"
  git push -q -u origin feature
  EXPECTED_HEAD="$(git rev-parse HEAD)"

  git checkout -q main
  git reset -q --hard origin/main
  echo "conflict from main" > conflicted.txt
  git add -A && git commit -qm "main changes conflicted.txt"
  git push -q origin main

  # Simulate the model running `git merge origin/main` with a conflicting file.
  git checkout -q feature
  git fetch -q origin
  set +e
  git merge origin/main >/dev/null 2>&1
  merge_status=$?
  set -e
  assert_eq "1" "$merge_status" "merge produced a conflict"
  assert_true "MERGE_HEAD exists" test -f .git/MERGE_HEAD

  git_state="$(unclean_git_state_json)"
  assert_eq "true" "$(jq -r .merge_in_progress <<< "$git_state")" "merge_in_progress detected"
  assert_eq "true" "$(jq -r .unmerged_entries <<< "$git_state")" "unmerged_entries detected"
  assert_true "conflict markers detected" conflict_markers_present

  decision="$(python3 "$TRUSTED_GUARD" \
    --previous-json '[]' \
    --latest-json '["conflicted.txt"]' \
    --git-state "$git_state" \
    --title "Product bug fix" \
    --body "Normal product issue")"
  assert_eq "true" "$(jq -r .reject <<< "$decision")" "trusted guard rejects unclean merge state"
  assert_eq "unclean-git-state" "$(jq -r .reason <<< "$decision")" "rejection reason is unclean-git-state"

  # Persistence must reset without commit or push: HEAD must return to the
  # checked-out target and no merge state may remain.
  git reset --hard "$EXPECTED_HEAD" >/dev/null
  git clean -fd >/dev/null
  assert_eq "$EXPECTED_HEAD" "$(git rev-parse HEAD)" "HEAD reset to expected target"
  assert_false "no MERGE_HEAD remains" test -f .git/MERGE_HEAD
}

# ===========================================================================
# REGRESSION 3: completed model merge/rebase of main is rejected.
# ===========================================================================
test_regression_model_committed_merge() {
  printf '\n--- test_regression_model_committed_merge ---\n'
  new_work
  extract_functions stage_trusted_guard unclean_git_state_json branch_folded_main_commits

  cd "$TEST_TMPDIR/work"
  mkdir -p .github/scripts
  cp "$TRUSTED_GUARD_SRC" .github/scripts/fixed-model-guard.py
  git add -A && git commit -qm "add trusted guard"
  git push -q origin main
  TRUSTED_GUARD="${TEST_TMPDIR}/trusted-fixed-model-guard.py"
  stage_trusted_guard

  git checkout -q -b feature
  git reset -q --hard origin/main
  echo "feature work" > feature.txt
  git add -A && git commit -qm "feature work"
  git push -q -u origin feature
  EXPECTED_HEAD="$(git rev-parse HEAD)"

  git checkout -q main
  git reset -q --hard origin/main
  echo "main work" > mainfile.txt
  git add -A && git commit -qm "advance main"
  git push -q origin main

  git checkout -q feature
  git fetch -q origin

  # A clean merge of origin/main moves HEAD and folds main commits in.
  git merge origin/main -m "model merges main" >/dev/null 2>&1
  git_state="$(unclean_git_state_json)"
  assert_eq "true" "$(jq -r .head_changed <<< "$git_state")" "head_changed detected after model merge"
  assert_eq "true" "$(jq -r .foreign_main_commits <<< "$git_state")" "foreign_main_commits detected after model merge"

  git reset -q --hard "$EXPECTED_HEAD"
  git rebase origin/main >/dev/null 2>&1
  git_state="$(unclean_git_state_json)"
  assert_eq "true" "$(jq -r .head_changed <<< "$git_state")" "head_changed detected after model rebase"
  assert_eq "true" "$(jq -r .foreign_main_commits <<< "$git_state")" "foreign_main_commits detected after model rebase"
  git reset -q --hard "$EXPECTED_HEAD"
}

test_worker_fail_fast_env() {
  printf '\n--- test_worker_fail_fast_env ---\n'
  # The worker must refuse to start when required identity env vars are
  # missing. The test harness provides no factory env vars here on purpose;
  # this proves fail-fast does not depend on weakened production defaults.
  local output
  output="$(env -i HOME="$HOME" PATH="$PATH" bash "$WORKER" 2>&1 || true)"
  if grep -q 'FACTORY_WORKER is required' <<< "$output"; then
    pass "worker fails fast on missing FACTORY_WORKER"
  else
    fail "worker did not fail fast on missing FACTORY_WORKER"
  fi
}

test_worker_syntax() {
  printf '\n--- test_worker_syntax ---\n'
  bash -n "$WORKER" && pass "worker script has valid bash syntax" || fail "worker script has syntax errors"
  bash -n "$ROOT/.github/scripts/omniroute-factory-worker.sh" && pass "omniroute worker syntax OK" || fail "omniroute worker syntax"
  bash -n "$ROOT/.github/scripts/nvidia-factory-worker.sh" && pass "nvidia worker syntax OK" || fail "nvidia worker syntax"
}

test_worker_syntax
test_worker_fail_fast_env
test_trusted_guard_staging
test_regression_stale_branch_guard
test_regression_merge_main_rejected
test_regression_model_committed_merge

printf '\n==============================\n'
printf 'Results: %d passed, %d failed\n' "$PASS" "$FAIL"
printf '==============================\n'

if ((FAIL > 0)); then
  exit 1
fi
exit 0
