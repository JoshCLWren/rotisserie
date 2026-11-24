"""Regression coverage for the factory status GitHub Pages workflow."""

from pathlib import Path

from tests.workflow_files import adopter_view, own_steps


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "factory-status-pages.yml"


def test_pages_workflow_uses_supported_first_deploy_configuration() -> None:
    """Keep the Pages workflow compatible with repositories enabled for Actions publishing."""
    workflow = adopter_view(WORKFLOW.name)

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
    workflow = adopter_view(WORKFLOW.name)

    assert "factory-status-live" in workflow
    assert "Publish live dashboard snapshot" in workflow
    assert 'if: github.event_name == \'push\'' in workflow
    assert "repos/${GITHUB_REPOSITORY}/contents/index.html" in workflow
    assert "raw.githubusercontent.com/${{ github.repository }}/factory-status-live/index.html" in workflow
    assert "cache: 'no-store'" in workflow
    assert 'http-equiv="refresh" content="60"' in workflow


def test_pages_workflow_proves_the_shell_and_live_snapshot_are_available() -> None:
    """Do not treat an accepted Pages artifact as proof that users can see live data."""
    workflow = adopter_view(WORKFLOW.name)

    assert "Verify Pages publishing mode" in workflow
    assert 'build_type\" != \"workflow\"' in workflow
    assert "Verify public shell and live snapshot" in workflow
    assert "factory-pages-check=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in workflow
    assert "factory-live-check=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in workflow
    assert "live snapshot is stale" in workflow


def test_pages_workflow_probes_opencode_free_roster_fail_soft() -> None:
    """Refresh the roster with OmniRoute credentials without failing the page."""
    workflow = adopter_view(WORKFLOW.name)

    assert "OMNIROUTE_API_KEY: ${{ secrets.OMNIROUTE_API_KEY }}" in workflow
    assert (
        "OMNIROUTE_MANAGEMENT_API_KEY: ${{ secrets.OMNIROUTE_MANAGEMENT_API_KEY }}"
        in workflow
    )
    assert "OMNIROUTE_BASE_URL: ${{ vars.OMNIROUTE_BASE_URL }}" in workflow
    assert "generate_factory_status_dashboard.py" in workflow


def test_large_dashboard_upload_uses_file_input_not_process_arguments() -> None:
    """Huge fleet dashboards must not hit Linux's per-argument size limit."""
    workflow = adopter_view(WORKFLOW.name)
    assert "publish_snapshot site/index.html index.html" in workflow
    assert "publish_snapshot site/funnel-cache.json funnel-cache.json" in workflow
    assert 'base64.b64encode(Path(source).read_bytes())' in workflow
    assert '--input "$payload_file"' in workflow
    assert '-f "content=\u0024{content}"' not in workflow
    assert '-f "content=\u0024(base64 -w0 site/funnel-cache.json)"' not in workflow


def test_publisher_also_runs_after_the_scheduler_when_the_snapshot_is_stale() -> None:
    """GitHub throttled the */15 schedule (3 runs in ~14 hours); stats went stale."""
    import yaml

    workflow = yaml.safe_load(adopter_view(WORKFLOW.name))
    triggers = workflow[True]  # PyYAML reads the bare `on:` key as True
    assert triggers["workflow_run"] == {
        "workflows": ["Fixed Model Factory Dispatcher"],
        "types": ["completed"],
    }
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    jobs = workflow["jobs"]
    assert jobs["publish-live-dashboard"]["needs"] == "freshness"
    assert jobs["publish-live-dashboard"]["if"] == "needs.freshness.outputs.stale == 'true'"
    gate = own_steps(jobs["freshness"])[0]["run"]
    assert 'if [[ "$EVENT" != "workflow_run" ]]' in gate  # manual and cron runs always publish
    assert "age > 840" in gate


def test_refresh_runs_cannot_cancel_a_queued_push_deployment() -> None:
    """Greptile P1 on adopter PR #5671: one pending run per concurrency group."""
    import yaml

    workflow = yaml.safe_load(adopter_view(WORKFLOW.name))
    group = workflow["concurrency"]["group"]
    assert group == (
        "factory-status-pages-${{ github.event_name == 'push' && 'push' || 'refresh' }}"
    )
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_published_stats_carry_the_workers_model_exclusions() -> None:
    """`baste unstick` reads the worker's own exclusions; publish them only after a live read."""
    workflow = adopter_view(WORKFLOW.name)

    assert "id: reliability" in workflow
    assert "if: steps.reliability.outcome == 'success'" in workflow
    assert "factory_state_operations.py export-model-exclusions" in workflow
    assert ".model_exclusions = {generated_at: $at, sources: $sources[0]}" in workflow
