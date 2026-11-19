"""Publisher must consume finalized, authenticated quota observations."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_publisher_runs_after_observation_not_before_it() -> None:
    """The :10 publisher ran before the :17 observer and reported stale samples."""
    publisher = (WORKFLOWS / "publish-factory-api-budgets.yml").read_text(
        encoding="utf-8"
    )
    observer = (WORKFLOWS / "factory-api-quota-observation.yml").read_text(
        encoding="utf-8"
    )

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
    publisher = (WORKFLOWS / "publish-factory-api-budgets.yml").read_text(
        encoding="utf-8"
    )
    assert "vars.FACTORY_OBSERVABILITY_ENABLED == 'true'" in publisher
    assert "github.ref == 'refs/heads/main'" in publisher
    assert "permission-actions: read" in publisher
    assert "permission-contents: write" in publisher
    assert "python3 -m scripts.sync_factory_quota" in publisher
