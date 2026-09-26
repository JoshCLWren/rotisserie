"""Regression coverage for the fixed-model semantic verdict boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".github/scripts/factory-semantic-verdict.sh"
WORKER = ROOT / ".github/scripts/free-model-factory-worker.sh"


def run_bash(script: str) -> subprocess.CompletedProcess[str]:
    """Run a focused shell assertion against the trusted helper."""
    return subprocess.run(
        ["bash", "-c", f"source {HELPER!s}; {script}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def verdict_for(tmp_path: Path, text: str) -> tuple[int, str]:
    log = tmp_path / "review.log"
    log.write_text(text)
    result = run_bash(f"factory_extract_semantic_verdict {log!s}")
    return result.returncode, result.stdout.strip()


def test_exact_ready_is_approval(tmp_path: Path) -> None:
    assert verdict_for(tmp_path, "reviewed files\nFACTORY_GATE_READY\n") == (0, "approve")


def test_blocked_and_legacy_not_ready_fail_closed(tmp_path: Path) -> None:
    assert verdict_for(tmp_path, "reviewed files\nFACTORY_GATE_BLOCKED\n") == (0, "repair")
    assert verdict_for(tmp_path, "reviewed files\nFACTORY_GATE_NOT_READY\n") == (0, "repair")


def test_harmless_markdown_wrapper_is_accepted_only_on_final_line(tmp_path: Path) -> None:
    assert verdict_for(tmp_path, "analysis\n**FACTORY_GATE_READY**\n") == (0, "approve")


def test_prompt_echo_before_final_line_cannot_approve(tmp_path: Path) -> None:
    code, verdict = verdict_for(
        tmp_path,
        "The instructions mention FACTORY_GATE_READY and FACTORY_GATE_BLOCKED.\nStill reviewing.\n",
    )
    assert code != 0
    assert verdict == ""


def test_marker_inside_code_fence_is_not_terminal_approval(tmp_path: Path) -> None:
    code, verdict = verdict_for(
        tmp_path,
        "Example:\n```\nFACTORY_GATE_READY\n```\nConclusion omitted.\n",
    )
    assert code != 0
    assert verdict == ""


def test_truncated_marker_fails_closed(tmp_path: Path) -> None:
    code, verdict = verdict_for(tmp_path, "analysis\nFACTORY_GATE_REA")
    assert code != 0
    assert verdict == ""


def test_conflicting_markers_are_detected(tmp_path: Path) -> None:
    log = tmp_path / "review.log"
    log.write_text("FACTORY_GATE_READY\nmore reasoning\nFACTORY_GATE_BLOCKED\n")
    result = run_bash(f"factory_review_has_conflicting_terminal_markers {log!s}")
    assert result.returncode == 0


def test_ready_recovery_ignores_non_authoritative_primary_prose(tmp_path: Path) -> None:
    negative_prose = tmp_path / "negative-prose.log"
    negative_prose.write_text(
        "Inspected app/api/roll.py. There is no blocking issue and the PR is safe.\n"
        "The protocol names FACTORY_GATE_BLOCKED when repairs are needed.\n"
        "Summary accidentally followed the verdict.\n"
    )
    explicit_deny = tmp_path / "explicit-deny.log"
    explicit_deny.write_text("Review found a defect.\nFACTORY_GATE_BLOCKED\n")

    allowed = run_bash(
        f"factory_primary_review_denies_ready_recovery {negative_prose!s}"
    )
    denied = run_bash(f"factory_primary_review_denies_ready_recovery {explicit_deny!s}")

    assert allowed.returncode != 0
    assert denied.returncode == 0


def test_sanitizer_redacts_git_and_bearer_credentials(tmp_path: Path) -> None:
    source = tmp_path / "raw.log"
    target = tmp_path / "safe.log"
    source.write_text(
        "origin https://x-access-token:github_pat_secret123@github.com/JoshCLWren/comic-pile.git\n"
        "Authorization: Bearer super-secret-value\n"
    )
    result = run_bash(f"factory_sanitize_review_log {source!s} {target!s}")
    assert result.returncode == 0
    safe = target.read_text()
    assert "github_pat_secret123" not in safe
    assert "super-secret-value" not in safe
    assert "[REDACTED]" in safe


def test_worker_recovery_is_fresh_bounded_and_fail_closed() -> None:
    worker = WORKER.read_text()
    helper = HELPER.read_text()

    assert "opencode run -m \"$runtime_model\"" in helper
    assert "gh pr status <number>" in helper
    assert "Return exactly one bare line" in helper
    assert "FACTORY_GATE_BLOCKED" in helper
    assert "recovery_timeout=90" in worker
    assert "current_head" in worker
    assert "current_owner_is_self" in worker
    assert "semantic-review-recovery-failed" in worker
    assert "factory_review_has_conflicting_terminal_markers" in worker
    assert "factory_sanitize_review_log" in worker
    assert '--review-log "$sanitized_review_log"' in worker


def test_repair_lease_does_not_enter_review_controller_without_handoff() -> None:
    """A no-change repair must remain repair work instead of crashing review validation."""
    worker = WORKER.read_text()

    assert "ASSIGNED_PR_STAGE" in worker
    repair_guard = "if [[ \"$ASSIGNED_PR_STAGE\" != 'factory:review' ]]; then"
    assert repair_guard in worker
    guard_index = worker.index(repair_guard)
    controller_index = worker.index('python3 "$TRUSTED_REVIEW_CONTROLLER" review')
    assert guard_index < controller_index
    assert "repair-no-persisted-change-handoff" in worker
    assert "release_pr_and_issue \"$NUMBER\" \"$BRANCH\" \"$ASSIGNED_PR_STAGE\"" in worker


def test_status_vocabulary_is_canonical_and_includes_recovery_failure() -> None:
    helper = HELPER.read_text()
    expected = {
        "semantic_review_approved",
        "semantic_review_blocked",
        "semantic_review_verdict_recovered",
        "semantic_review_missing_verdict",
        "semantic_review_conflicting_verdict",
        "semantic_review_recovery_failed",
        "semantic_review_timeout",
        "semantic_review_model_error",
        "semantic_review_head_changed",
    }
    for status in expected:
        assert helper.count(status) == 1
