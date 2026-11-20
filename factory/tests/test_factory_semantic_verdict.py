"""Regression coverage for the fixed-model semantic verdict boundary."""

from __future__ import annotations

import json
import shlex
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
    assert "The final line must be exactly one bare verdict marker" in helper
    assert "pull request #${pr} at exact head ${head}" in helper
    assert '"$NUMBER" "$EXPECTED_HEAD" "$acceptance_mission"' in worker
    assert "factory_append_recovered_acceptance_report" in worker
    assert 'factory_review_has_conflicting_terminal_markers "$sanitized_recovery_log"' in worker
    assert 'factory_sanitize_review_log "$recovery_log" "$sanitized_recovery_log"' in worker
    assert '"$sanitized_review_log" "$sanitized_recovery_log"' in worker
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


POLICY = ROOT / ".github/scripts/factory_issue_acceptance.py"
RECOVERY_HEAD = "a" * 40


def recovery_report(head: str = RECOVERY_HEAD) -> dict[str, object]:
    """Model evidence fixture; the trusted controller validates issue criteria."""
    return {
        "issue": 4916,
        "head": head,
        "issue_digest": "b" * 64,
        "items": [{
            "id": "A1",
            "status": "pass",
            "file": ".github/scripts/factory-semantic-verdict.sh",
            "evidence": "Inspected actual review recovery handling and preserved acceptance",
            "test": "pytest tests/test_factory_semantic_verdict.py verifies evidence handoff",
        }],
    }


def test_recovery_appends_one_model_report_when_original_has_none(tmp_path: Path) -> None:
    primary = tmp_path / "primary.log"
    recovery = tmp_path / "recovery.log"
    primary.write_text("Inspected current PR diff and issue; conclusion pending.\n")
    recovery.write_text(
        "Inspected the current PR, review findings and source.\n"
        + "FACTORY_ACCEPTANCE_V1=" + json.dumps(recovery_report()) + "\n"
        + "FACTORY_GATE_READY\n"
    )
    call = (
        "factory_append_recovered_acceptance_report "
        f"{primary} {recovery} {POLICY} {RECOVERY_HEAD}"
    )
    assert run_bash(call).returncode == 0
    assert run_bash(call).returncode == 0
    assert primary.read_text().count("FACTORY_ACCEPTANCE_V1=") == 1
    assert run_bash(f"factory_has_acceptance_report {primary} {POLICY}").returncode == 0


def test_recovery_preserves_existing_primary_report(tmp_path: Path) -> None:
    primary = tmp_path / "primary.log"
    recovery = tmp_path / "recovery.log"
    original = "FACTORY_ACCEPTANCE_V1=" + json.dumps(recovery_report()) + "\n"
    primary.write_text(original)
    recovery.write_text(
        "FACTORY_ACCEPTANCE_V1=" + json.dumps(recovery_report("c" * 40)) + "\n"
    )
    call = (
        "factory_append_recovered_acceptance_report "
        f"{primary} {recovery} {POLICY} {RECOVERY_HEAD}"
    )
    assert run_bash(call).returncode == 0
    assert primary.read_text() == original


def test_recovery_rejects_malformed_primary_and_multiple_reports(tmp_path: Path) -> None:
    primary = tmp_path / "primary.log"
    recovery = tmp_path / "recovery.log"
    valid = "FACTORY_ACCEPTANCE_V1=" + json.dumps(recovery_report()) + "\n"
    primary.write_text("FACTORY_ACCEPTANCE_V1=not-json\n")
    recovery.write_text(valid)
    call = (
        "factory_append_recovered_acceptance_report "
        f"{primary} {recovery} {POLICY} {RECOVERY_HEAD}"
    )
    assert run_bash(call).returncode != 0
    assert primary.read_text() == "FACTORY_ACCEPTANCE_V1=not-json\n"

    primary.write_text("No report present.\n")
    recovery.write_text(valid + valid)
    assert run_bash(call).returncode != 0
    assert primary.read_text() == "No report present.\n"


def test_recovery_rejects_stale_head_and_absent_evidence(tmp_path: Path) -> None:
    primary = tmp_path / "primary.log"
    recovery = tmp_path / "recovery.log"
    primary.write_text("Only unverified prose.\n")
    recovery.write_text(
        "FACTORY_ACCEPTANCE_V1=" + json.dumps(recovery_report("c" * 40)) + "\n"
    )
    call = (
        "factory_append_recovered_acceptance_report "
        f"{primary} {recovery} {POLICY} {RECOVERY_HEAD}"
    )
    assert run_bash(call).returncode != 0
    recovery.write_text("FACTORY_GATE_READY\n")
    assert run_bash(call).returncode != 0
    assert run_bash(f"factory_has_acceptance_report {primary} {POLICY}").returncode != 0


def test_recover_verdict_rejects_invalid_pr_and_head(tmp_path: Path) -> None:
    """The recovery model is bound to an exact PR number and 40-char lowercase head."""
    recovery_log = str(tmp_path / "recovery.log")
    head40 = "a" * 40
    # PR must be a positive integer (not zero, not negative, not non-numeric)
    for bad_pr in ("0", "-1", "abc"):
        result = run_bash(
            f"factory_recover_semantic_verdict model workspace 30 {recovery_log} '{bad_pr}' {head40} 'mission'"
        )
        assert result.returncode == 2, f"PR={bad_pr!r} should be rejected, got {result.returncode}"
    # Head must be exactly 40 lowercase hex characters (not short, not uppercase, not non-hex)
    for bad_head in ("short", "A" * 40, "g" * 40, "a" * 39, "a" * 41):
        result = run_bash(
            f"factory_recover_semantic_verdict model workspace 30 {recovery_log} 1 {bad_head} 'mission'"
        )
        assert result.returncode == 2, f"head={bad_head!r} should be rejected, got {result.returncode}"


def test_recovery_reads_primary_ready_with_no_report(tmp_path: Path) -> None:
    """A primary READY result with no parseable report is handled correctly during worker handoff."""
    primary = tmp_path / "primary.log"
    recovery = tmp_path / "recovery.log"
    # Primary has READY marker but no FACTORY_ACCEPTANCE_V1 report
    primary.write_text("FACTORY_GATE_READY\n")
    recovery.write_text(
        "FACTORY_ACCEPTANCE_V1=" + json.dumps(recovery_report()) + "\n"
        + "FACTORY_GATE_READY\n"
    )
    call = (
        "factory_append_recovered_acceptance_report "
        f"{primary} {recovery} {POLICY} {RECOVERY_HEAD}"
    )
    assert run_bash(call).returncode == 0
    assert primary.read_text().count("FACTORY_ACCEPTANCE_V1=") == 1
    assert run_bash(f"factory_has_acceptance_report {primary} {POLICY}").returncode == 0


def test_composite_recovery_handoff_requires_complete_trusted_issue_rubric(
    tmp_path: Path,
) -> None:
    """Reproduce #4926: bare JSON/A1-only READY is not independent acceptance."""
    mission = (
        f"ISSUE ACCEPTANCE: #4916, head {RECOVERY_HEAD}, issue digest {'b' * 64}.\n"
        "ACCEPTANCE_CRITERIA_COUNT=3\n"
    )
    # Three *independently described* entries; no wrapper may generate these.
    complete = recovery_report()
    items = complete["items"]
    assert isinstance(items, list)
    original_item = items[0]
    assert isinstance(original_item, dict)
    complete["items"] = [
        {**original_item, "id": f"A{i}"}
        for i in range(1, 4)
    ]
    primary = tmp_path / "primary.log"
    recovery = tmp_path / "recovery.log"
    primary.write_text("FACTORY_GATE_READY\n")
    call = (
        "factory_append_recovered_acceptance_report "
        f"{shlex.quote(str(primary))} {shlex.quote(str(recovery))} "
        f"{shlex.quote(str(POLICY))} {RECOVERY_HEAD} {shlex.quote(mission)}"
    )
    check = (
        "factory_has_acceptance_report "
        f"{shlex.quote(str(primary))} {shlex.quote(str(POLICY))} {shlex.quote(mission)}"
    )

    # The actual OpenRouter recovery emitted this bare, one-criterion JSON.
    recovery.write_text(json.dumps(recovery_report()) + "\nFACTORY_GATE_READY\n")
    assert run_bash(call).returncode != 0
    assert run_bash(check).returncode != 0
    assert primary.read_text() == "FACTORY_GATE_READY\n"

    # Even prefixed evidence is incomplete if it does not cover A1..A3.
    recovery.write_text("FACTORY_ACCEPTANCE_V1=" + json.dumps(recovery_report()) + "\n")
    assert run_bash(call).returncode != 0
    assert primary.read_text() == "FACTORY_GATE_READY\n"

    # Exactly one model-authored, prefixed, correctly shaped report flows once.
    recovery.write_text("FACTORY_ACCEPTANCE_V1=" + json.dumps(complete) + "\nFACTORY_GATE_READY\n")
    assert run_bash(call).returncode == 0
    assert run_bash(call).returncode == 0
    assert run_bash(check).returncode == 0
    assert primary.read_text().count("FACTORY_ACCEPTANCE_V1=") == 1


def test_worker_checks_full_mission_before_recovered_ready() -> None:
    worker = WORKER.read_text()
    helper = HELPER.read_text()
    mission = 'acceptance_mission="$(factory_issue_acceptance_mission "$NUMBER" "$EXPECTED_HEAD")"'
    assert worker.count(mission) == 1
    assert worker.index(mission) < worker.index('if [[ "$verdict" == \'approve\' ]]')
    assert 'factory_has_acceptance_report "$sanitized_review_log" "$acceptance_policy" "$acceptance_mission"' in worker
    assert '"$EXPECTED_HEAD" "$acceptance_mission"; then' in worker
    assert "policy.report_matches_mission(recovered, sys.argv[5])" in helper
    assert "A bare JSON object" in helper
    assert "recovery_timeout=90" in worker
    assert "recovery_timeout=180" in worker
    assert 'if [[ "$RUNTIME_MODEL" == \'openrouter/free\' ]]; then' in worker


def test_failed_recovery_does_not_reenable_errexit_in_the_worker(tmp_path: Path) -> None:
    """Run 44071823893: a 124 recovery exit killed the worker before release.

    The helper used to finish with `set -e`, which re-armed errexit inside the
    caller's `set +e` window, so the non-zero return terminated the worker.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\necho 'partial review'\nexit 124\n")
    fake.chmod(0o755)
    recovery_log = tmp_path / "recovery.log"
    head = "a" * 40
    script = (
        f"set -Eeuo pipefail; source {HELPER!s}; export PATH={bindir!s}:$PATH; "
        "set +e; "
        f"factory_recover_semantic_verdict model {tmp_path!s} 30 {recovery_log!s} 5299 {head} m; "
        "rc=$?; set -e; echo \"survived rc=$rc\"; "
        "rc=0; "
        f"factory_recover_semantic_verdict model {tmp_path!s} 30 {recovery_log!s} 5299 {head} m "
        "|| rc=$?; echo \"survived again rc=$rc\""
    )
    result = subprocess.run(["bash", "-c", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "survived rc=124" in result.stdout
    assert "survived again rc=124" in result.stdout
    assert "partial review" in recovery_log.read_text()


def test_worker_fails_over_on_zero_exit_provider_account_refusal() -> None:
    """Pollinations credit refusals exit 0; the worker must treat them as failures."""
    text = WORKER.read_text(encoding="utf-8")
    assert "(does|do)(n.t| not) have enough credits" in text
    loop = text.split('log "agent exit status ${agent_status} for ${MODE} #${NUMBER}"', 1)[1]
    guard = loop.split("if (( agent_status == 0 )); then", 1)[0]
    assert 'zero_exit_provider_refusal "$attempt_elapsed"' in guard
    assert "agent_status=1" in guard
    assert "|| recovery_status=$?" in text
