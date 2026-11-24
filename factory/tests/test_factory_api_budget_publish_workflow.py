"""Publisher must consume finalized, authenticated quota observations."""

from pathlib import Path

from tests.workflow_files import adopter_view


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT.parent / ".github" / "workflows"


def test_publisher_runs_after_observation_not_before_it() -> None:
    """The :10 publisher ran before the :17 observer and reported stale samples."""
    publisher = adopter_view("publish-factory-api-budgets.yml")
    observer = adopter_view("factory-api-quota-observation.yml")

    assert "name: Factory API Quota Observation" in observer
    assert "  workflow_run:" in publisher
    assert "workflows: ['Factory API Quota Observation']" in publisher
    assert "types: [completed]" in publisher
    assert "  schedule:" not in publisher
    assert "github.event.workflow_run.conclusion == 'success'" in publisher
    assert "github.event.workflow_run.head_branch == 'main'" in publisher
    assert "github.event_name == 'workflow_dispatch'" in publisher


def test_publisher_remains_explicitly_enabled_and_separately_scoped() -> None:
    """Event-driven publishing must not loosen source or destination permissions."""
    publisher = adopter_view("publish-factory-api-budgets.yml")
    assert "vars.FACTORY_OBSERVABILITY_ENABLED == 'true'" in publisher
    assert "github.ref == 'refs/heads/main'" in publisher
    assert "permission-actions: read" in publisher
    assert "permission-contents: write" in publisher
    assert "python3 -m scripts.sync_factory_quota" in publisher
