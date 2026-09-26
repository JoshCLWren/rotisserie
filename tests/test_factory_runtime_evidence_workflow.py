"""Regression coverage for durable factory attempt evidence in the runner workflow."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "free-model-factory-run.yml"
WORKER = ROOT / ".github" / "scripts" / "free-model-factory-worker.sh"


def test_terminal_step_publishes_normalized_attempt_evidence():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "factory-attempt-outcome:v1" in workflow
    assert "Attempt outcome: %s" in workflow
    assert "attempt_outcome='success'" in workflow
    assert "attempt_outcome='control_plane_failure'" in workflow
    assert "attempt_outcome='unknown_failure'" in workflow


def test_worker_status_is_captured_after_attempt_code_exits():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")

    assert 'printf \'%s\\n\' "$worker_status" > "$RUNNER_TEMP/factory-worker-status"' in workflow
    assert 'worker_status="$(cat "$WORKER_STATUS_FILE" 2>/dev/null || true)"' in workflow
    assert 'worker_status" == 2 || "$worker_status" == 3' in workflow
    assert "Exit 2 and 3 are reserved for controller invariant/read failures" in worker


def test_terminal_step_preserves_newer_attempt_evidence():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "existing_run > GITHUB_RUN_ID" in workflow
    assert "Newer attempt evidence from run" in workflow


def test_omniroute_control_plane_requests_retry_transient_tunnel_failures():
    """A brief Funnel/DNS interruption must not retire the gateway for one probe."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("--retry 5 --retry-all-errors --retry-delay 5") == 1
