"""Regression coverage for durable factory attempt evidence in the runner workflow."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "free-model-factory-run.yml"
WORKER = ROOT / ".github" / "scripts" / "free-model-factory-worker.sh"


def test_terminal_step_summarizes_normalized_attempt_evidence() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    # Issue #1093 is retired: the provisional outcome is a job summary only;
    # the trusted Entry persister classifies the job log into Neon.
    assert "factory-attempt-outcome:v1" not in workflow
    assert "Provisional attempt outcome: `%s`" in workflow
    assert "attempt_outcome='classification_pending'" in workflow
    assert "attempt_outcome='success'" not in workflow
    entry = (ROOT / ".github/workflows/free-model-factory-entry.yml").read_text()
    assert 'verdict="code_delivered"' in entry
    assert 'retry_gh_api "$RUNNER_TEMP/factory-delivery-pr.json"' in entry
    assert '"repos/$GITHUB_REPOSITORY/pulls/$delivery_pr"' in entry
    assert '.head.repo.full_name == $repo' in entry
    assert "attempt_outcome='control_plane_failure'" in workflow
    assert "attempt_outcome='unknown_failure'" in workflow


def test_worker_status_is_captured_after_attempt_code_exits() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")

    assert 'printf \'%s\\n\' "$worker_status" > "$RUNNER_TEMP/factory-worker-status"' in workflow
    assert 'worker_status="$(cat "$WORKER_STATUS_FILE" 2>/dev/null || true)"' in workflow
    assert 'worker_status" == 2 || "$worker_status" == 3' in workflow
    assert "Exit 2 and 3 are reserved for controller invariant/read failures" in worker


def test_newer_attempt_evidence_is_ordered_by_run_in_neon() -> None:
    """Run ordering moved from comment bodies to Neon's run-keyed projections."""
    operations = (ROOT / ".github/scripts/factory_state_operations.py").read_text()
    assert "last_run_id <= EXCLUDED.last_run_id" in operations
    assert "existing_run > GITHUB_RUN_ID" not in WORKFLOW.read_text(encoding="utf-8")


def test_omniroute_control_plane_requests_retry_transient_tunnel_failures() -> None:
    """A brief Funnel/DNS interruption must not retire the gateway for one probe."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("--retry 5 --retry-all-errors --retry-delay 5") == 1
