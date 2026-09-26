"""Regression coverage for the factory status GitHub Pages workflow."""

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "factory-status-pages.yml"


def test_pages_workflow_uses_supported_first_deploy_configuration() -> None:
    """Keep the Pages workflow compatible with repositories enabled for Actions publishing."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "enablement: true" not in workflow
    assert "contents: write" in workflow
    assert "pages: write" in workflow
    assert "id-token: write" in workflow
    assert "actions/configure-pages@v6" in workflow
    assert "actions/upload-pages-artifact@v5" in workflow
    assert "actions/deploy-pages@v5" in workflow
    assert "cancel-in-progress: false" in workflow


def test_scheduled_refreshes_do_not_redeploy_pages_for_the_same_commit() -> None:
    """Publish live data separately because Pages deployments are keyed by commit SHA."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "factory-status-live" in workflow
    assert "Publish live dashboard snapshot" in workflow
    assert 'if: github.event_name == \'push\'' in workflow
    assert "repos/${GITHUB_REPOSITORY}/contents/index.html" in workflow
    assert "raw.githubusercontent.com/JoshCLWren/comic-pile/factory-status-live/index.html" in workflow
    assert "cache: 'no-store'" in workflow
    assert 'http-equiv="refresh" content="60"' in workflow


def test_pages_workflow_proves_the_shell_and_live_snapshot_are_available() -> None:
    """Do not treat an accepted Pages artifact as proof that users can see live data."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "Verify Pages publishing mode" in workflow
    assert 'build_type\" != \"workflow\"' in workflow
    assert "Verify public shell and live snapshot" in workflow
    assert "factory-pages-check=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in workflow
    assert "factory-live-check=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in workflow
    assert "live snapshot is stale" in workflow


def test_pages_workflow_probes_opencode_free_roster_fail_soft() -> None:
    """Refresh the roster with OmniRoute credentials without failing the page."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert ".github/scripts/probe_opencode_free_roster.py" in workflow
    assert "OMNIROUTE_API_KEY: ${{ secrets.OMNIROUTE_API_KEY }}" in workflow
    assert (
        "OMNIROUTE_MANAGEMENT_API_KEY: ${{ secrets.OMNIROUTE_MANAGEMENT_API_KEY }}"
        in workflow
    )
    assert "OMNIROUTE_BASE_URL: ${{ vars.OMNIROUTE_BASE_URL }}" in workflow
    assert "generate_factory_status_dashboard.py" in workflow
