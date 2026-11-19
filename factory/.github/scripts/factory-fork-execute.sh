#!/usr/bin/env bash
# Runs only in Factory Fork Execution: a separate read-only, secretless runner.
set -Eeuo pipefail
: "${TRUSTED_FORK_DIRECTORY:?trusted helper directory required}"
: "${REQUESTED_PR:?PR required}"
: "${EXPECTED_HEAD:?head required}"
: "${FACTORY_WORKER:?worker required}"
: "${FORK_RECEIPT_NONCE:?receipt nonce required}"
: "${RUNNER_TEMP:?runner temp required}"
python3 "$TRUSTED_FORK_DIRECTORY/factory_fork_control.py" verify-execution \
  --pr "$REQUESTED_PR" --head "$EXPECTED_HEAD" --worker "$FACTORY_WORKER" --nonce "$FORK_RECEIPT_NONCE"
python3 "$TRUSTED_FORK_DIRECTORY/factory_fork_control.py" context \
  --pr "$REQUESTED_PR" --head "$EXPECTED_HEAD" --output "$RUNNER_TEMP/fork-context.txt"

# No writer/provider credentials exist in this job. Keep the real fork ref and
# contributor parent, without introducing a same-named base-repository branch.
git fetch origin "refs/pull/${REQUESTED_PR}/head"
test "$(git rev-parse FETCH_HEAD)" = "$EXPECTED_HEAD"
git switch -C factory-fork-input "$EXPECTED_HEAD"
export HOME="$RUNNER_TEMP/fork-home"
mkdir -p "$HOME"
export SOURCE=kilo-auto WORKER="$FACTORY_WORKER" DISPLAY="Anonymous fork repair"
export MODEL=kilo-auto/free RUNTIME_MODEL=kilo/kilo-auto/free
export FACTORY_RUNTIME_MODEL="$RUNTIME_MODEL" OPENCODE_MODEL="$RUNTIME_MODEL"
prompt="Follow factory policy; fix actionable findings with regressions. This is an isolated read-only GitHub job: do not post comments, commit, push, merge, or alter Actions state. Edit the worktree and run tests; the trusted wrapper persists changes. No repository/provider secrets are available. Use only the pinned anonymous Kilo route. End with FACTORY_GATE_NOT_READY if required work remains. Run context: Repair PR #${REQUESTED_PR} at exact head ${EXPECTED_HEAD}; context: ${RUNNER_TEMP}/fork-context.txt (trusted policy and untrusted scope, source and findings)."
bash "$TRUSTED_FORK_DIRECTORY/kilo-auto-factory-run.sh" \
  900 "Contributor PR #${REQUESTED_PR}" "$prompt" "$RUNNER_TEMP/fork-agent.log"

test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"
test -z "$(git config --get core.hooksPath || true)"
results="$RUNNER_TEMP/fork-results"
mkdir -p "$results"
if [[ -z "$(git status --porcelain)" ]]; then
  printf '{"status":"no-change","head":"%s"}\n' "$EXPECTED_HEAD" > "$results/result.json"
  exit 0
fi
# These checks and normal commit hooks intentionally execute only in this VM.
source "$TRUSTED_FORK_DIRECTORY/factory-fast-checks.sh"
factory_run_fast_checks "$EXPECTED_HEAD" "$RUNNER_TEMP/fork-fast-checks.log"
# The trusted pre-commit hook always runs scripts/lint.sh, which needs the
# Python toolchain regardless of which files changed. Fast checks only run
# `uv sync` for Python diffs, so install it here before committing.
factory_ensure_commit_hook_toolchain
bash "$TRUSTED_FORK_DIRECTORY/install-git-hooks.sh"
git config user.name "ComicPile Factory ${FACTORY_WORKER}"
git config user.email "factory-${FACTORY_WORKER}@users.noreply.github.com"
git add -A
git commit -m "fix: address contributor PR #${REQUESTED_PR} feedback" -m "Model: ${RUNTIME_MODEL}"
test "$(git show --no-patch --format=%P HEAD)" = "$EXPECTED_HEAD"
git branch -f factory-fork-result HEAD
git bundle create "$results/repair.bundle" refs/heads/factory-fork-result "^${EXPECTED_HEAD}"
printf '{"status":"changed","head":"%s"}\n' "$EXPECTED_HEAD" > "$results/result.json"
