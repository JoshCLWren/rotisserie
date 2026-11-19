"""Contract tests for using Neon to avoid per-worker full comment scans."""
from pathlib import Path


WORKFLOW = Path(".github/workflows/fixed-model-runtime-observer.yml")


def test_only_trusted_workflow_run_publisher_receives_neon_cache() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    classifier, publisher = workflow.split("  publish-runtime-outcome:", 1)
    assert "factory-state-cache" not in classifier
    assert "NEON_API_KEY" not in classifier
    assert "github.event_name == 'workflow_run'" in publisher
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in publisher
    assert "ref: main" in publisher
    assert "uses: ./.github/actions/factory-state-cache" in publisher
    assert "if: steps.runtime.outputs.publish == 'true'" in publisher
    assert "secrets.NEON_API_KEY" in publisher


def test_observer_persists_neon_outcome_without_registry_comments() -> None:
    """Issue #1093 is retired: the observer's durable record is Neon only."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    publisher = workflow.split("  publish-runtime-outcome:", 1)[1]
    assert "issues/1093" not in publisher
    assert "issues/comments" not in publisher
    assert "__attempt_id" not in publisher
    assert "factory_registry_ingest" not in publisher
    assert "remember_verified_markers" not in publisher
    assert "factory_state_ledger.py runtime-outcome" in publisher
    assert "issues: write" not in workflow
