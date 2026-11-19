"""Same-lease recovery for failed, unpushed factory PR repairs."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / ".github/scripts/free-model-factory-worker-primitives.sh"
WORKER = ROOT / ".github/scripts/free-model-factory-worker.sh"


def _function(name: str) -> str:
    """Extract the real persistence function for a stubbed shell integration test."""
    source = PRIMITIVES.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", source, re.M | re.S)
    assert match is not None
    return match.group(0)


def _run(*, stage: str, mode: str, budget: int) -> subprocess.CompletedProcess[str]:
    """Run the production persistence code with deterministic Git, checks and agent."""
    script = r"""
set -Eeuo pipefail
EXPECTED_HEAD=base
WORKER=933
OWNER=factory:933
FACTORY_PUSH_BLOCKED_REASON=''
GITHUB_REPOSITORY=JoshCLWren/comic-pile
ASSIGNED_PR_STAGE='@STAGE@'
MODE='@MODE@'
BUDGET='@BUDGET@'
checks=0
scopes=0
corrected=0
remaining() { printf '%s\n' "$BUDGET"; }
log() { printf 'log:%s\n' "$*" >&2; }
reject_out_of_scope_diff() {
  scopes=$((scopes+1))
  if [[ "$MODE" == scope-rejected && "$scopes" -gt 1 ]]; then return 1; fi
  return 0
}
factory_run_fast_checks() {
  checks=$((checks+1))
  printf 'Trusted pre-push check failure on attempt %s\n' "$checks" > "$2"
  if [[ "$MODE" == pass-after-retry && "$checks" -eq 2 ]]; then return 0; fi
  return 1
}
run_agent() {
  corrected=$((corrected+1))
  [[ "$1" == pr && "$2" == 42 ]] || return 11
  [[ -f "$FACTORY_FAST_CHECK_FEEDBACK_FILE" ]] || return 12
  [[ "$3" -ge 300 && "$3" -le 900 ]] || return 13
  printf 'received-diagnostic\n' >&2
  return 0
}
factory_report_fast_check_failure() { printf 'reported-failure\n' >&2; }
factory_advance_display() { printf test; }
factory_push_credential() { printf 'pat\tplaceholder'; }
factory_git_push() { printf 'pushed\n' >&2; }
replace_labels() { printf 'stage:%s\n' "$3" >&2; }
git() {
  case "$1" in
    status) printf ' M repaired.rs\n' ;;
    rev-parse) printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n' ;;
    ls-remote) printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\trefs/heads/branch\n' ;;
    *) printf 'git:%s\n' "$1" >&2 ;;
  esac
}
@FUNCTION@
status=0
_persist_pr_changes_untraced 42 branch || status=$?
printf 'status=%s checks=%s scopes=%s corrected=%s reason=%s\n' \
  "$status" "$checks" "$scopes" "$corrected" "$FACTORY_PUSH_BLOCKED_REASON"
"""
    script = (
        script.replace("@STAGE@", stage)
        .replace("@MODE@", mode)
        .replace("@BUDGET@", str(budget))
        .replace("@FUNCTION@", _function("_persist_pr_changes_untraced"))
    )
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("stage", "mode", "budget", "counts", "succeeds"),
    [
        ("factory:changes-requested", "pass-after-retry", 1200, (2, 2, 1), True),
        ("factory:changes-requested", "always-fail", 1200, (2, 2, 1), False),
        ("factory:changes-requested", "scope-rejected", 1200, (1, 2, 1), False),
        ("factory:changes-requested", "pass-after-retry", 400, (1, 1, 0), False),
        ("factory:review", "pass-after-retry", 1200, (1, 1, 0), False),
    ],
)
def test_trusted_checks_retry_before_push(
    stage: str,
    mode: str,
    budget: int,
    counts: tuple[int, int, int],
    succeeds: bool,
) -> None:
    """Only a scoped, revalidated correction may reach the real push branch."""
    result = _run(stage=stage, mode=mode, budget=budget)
    assert result.returncode == 0, result.stderr
    checks, scopes, corrected = counts
    assert f"checks={checks} scopes={scopes} corrected={corrected}" in result.stdout
    if succeeds:
        assert "status=0" in result.stdout
        assert "git:add" in result.stderr
        assert "git:commit" in result.stderr
        assert "pushed" in result.stderr.splitlines()
        assert "stage:factory:review" in result.stderr
        assert "git:reset" not in result.stderr
        assert "reported-failure" not in result.stderr
    else:
        assert "status=1" in result.stdout
        assert "reason=fast-checks-failed" in result.stdout
        assert "git:reset" not in result.stderr
        assert "git:clean" not in result.stderr
        assert "reported-failure" in result.stderr
        assert "git:add" not in result.stderr
        assert "git:commit" not in result.stderr
        assert "pushed" not in result.stderr.splitlines()


def test_all_repair_agent_lanes_read_original_fast_check_diagnostics() -> None:
    """The OpenCode and Kilo routes both receive feedback in the same lease."""
    for path in (PRIMITIVES, WORKER):
        source = path.read_text(encoding="utf-8")
        assert "FACTORY_FAST_CHECK_FEEDBACK_FILE" in source
        assert "same-lease fast-check repair" in source
        assert "Do not commit or push" in source
