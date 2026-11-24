"""The trusted store connection step, its consumers and bounded retention (no network)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.workflow_files import adopter_view, own_steps

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT.parent / ".github" / "workflows"
CALLERS = ROOT / "install" / "callers"
READ_ONLY = {"rotisserie-operator-read.yml", "rotisserie-external-frontier.yml"}
STORE_STEP = "./.github/actions/factory-store"


def store_workflows() -> list[Path]:
    return sorted(p for p in WORKFLOWS.glob("*.yml") if STORE_STEP in p.read_text())


def test_store_step_masks_and_writes_only_a_private_token_file() -> None:
    action = (ROOT / ".github/actions/factory-store/action.yml").read_text()
    assert '::add-mask::$STORE_URL' in action and '::add-mask::$STORE_TOKEN' in action
    assert "umask 077" in action and 'chmod 600 "$token_file"' in action
    assert 'FACTORY_TURSO_TOKEN_FILE=$token_file' in action
    assert "FACTORY_TURSO_TOKEN=" not in action
    assert 'echo "$STORE_TOKEN"' not in action and "GITHUB_OUTPUT" not in action
    required = action.split('if [[ "${FACTORY_STATE_REQUIRED:-}" == "true" ]]; then', 1)[1]
    assert required.split("fi", 1)[0].strip().endswith("exit 1")


def test_every_store_job_removes_the_token_and_uses_the_right_token() -> None:
    found = store_workflows()
    assert len(found) == 14
    for path in found:
        text = path.read_text()
        doc = yaml.safe_load(text)
        token = "FACTORY_TURSO_READ_TOKEN" if path.name in READ_ONLY else "FACTORY_TURSO_TOKEN"
        assert f"token: ${{{{ secrets.{token} }}}}" in text, path.name
        assert "asyncpg" not in text and "FACTORY_STATE_DSN_FILE" not in text, path.name
        assert "factory-state-cache" not in text and "NEON_PROJECT_ID" not in text, path.name
        assert "FACTORY_STATE_REQUIRED: ${{ secrets.FACTORY_TURSO_URL != ''" in text, path.name
        for name, job in doc["jobs"].items():
            steps = own_steps(job)
            if any(step.get("uses") == STORE_STEP for step in steps):
                last = steps[-1]
                assert last["if"] == "always()", (path.name, name)
                assert last["run"] == 'rm -f "$RUNNER_TEMP/factory-store-token"', (path.name, name)


def test_callers_pass_store_secrets_and_neon_only_to_the_migration() -> None:
    for path in sorted(CALLERS.glob("*.yml")):
        text = path.read_text()
        if path.name != "factory-store-migrate.yml":
            assert "NEON_API_KEY" not in text, path.name
        name = path.name
        if (WORKFLOWS / name).exists() and STORE_STEP in (WORKFLOWS / name).read_text():
            token = "FACTORY_TURSO_READ_TOKEN" if name in READ_ONLY else "FACTORY_TURSO_TOKEN"
            assert f"{token}: ${{{{ secrets.{token} }}}}" in text, name
            assert "FACTORY_TURSO_URL: ${{ secrets.FACTORY_TURSO_URL }}" in text, name
        if name in READ_ONLY:
            assert "FACTORY_TURSO_TOKEN:" not in text, name


def test_step_env_never_blanks_the_store_url() -> None:
    for path in store_workflows():
        assert not re.search(r"FACTORY_TURSO_URL: \$\{\{ inputs\.", path.read_text()), path.name


def test_workflows_inject_database_only_into_trusted_jobs():
    root = ROOT
    for filename in (
        "fixed-model-factory-dispatch.yml",
        "factory-status-pages.yml",
        "fixed-model-factory-dispatch-recovery.yml",
    ):
        workflow = (root.parent / ".github/workflows" / filename).read_text()
        assert f"uses: {STORE_STEP}" in workflow
        assert "secrets.FACTORY_TURSO_TOKEN" in workflow
    # The local independent reviewer must never receive database credentials.
    local_review = adopter_view("local-ollama-review.yml")
    assert "factory-store" not in local_review
    assert "FACTORY_TURSO" not in local_review

    # In the reusable workflow only the separate trusted cleanup runner can
    # reach the store, not the agent/model execution job.
    import yaml

    reusable = yaml.safe_load(
        adopter_view("free-model-factory-run.yml")
    )
    model_job = reusable["jobs"]["factory"]
    cleanup_job = reusable["jobs"]["release_pre_session_lease"]
    completed_cleanup_job = reusable["jobs"]["release_post_session_lease"]
    assert "factory-store" not in str(model_job)
    assert "FACTORY_TURSO" not in str(model_job)
    for trusted_job in (cleanup_job, completed_cleanup_job):
        assert any(
            step.get("uses") == STORE_STEP
            for step in own_steps(trusted_job)
        )

    # The trusted Entry snapshot job reads the store; the reusable worker never does.
    entry = adopter_view("free-model-factory-entry.yml")
    snapshot, collector = entry.split("\n  persist_outcome:", 1)
    preparation = reusable["jobs"]["validate_assignment"]
    assert model_job["needs"] == "validate_assignment"
    assert any(step.get("uses") == STORE_STEP for step in own_steps(preparation))
    assert model_job["env"]["FACTORY_MODEL_EXCLUSIONS"] == "${{ needs.validate_assignment.outputs.exclusions }}"
    assert "FACTORY_TURSO_TOKEN_FILE" not in snapshot
    assert "factory-store" in collector
    # The collector always waits for the worker. Check the dependency graph, not a YAML spelling that changed from a
    # scalar to a list when that second dependency was introduced.
    entry_jobs = yaml.safe_load(entry)["jobs"]
    collector_job = entry_jobs["persist_outcome"]
    needs = collector_job["needs"]
    needs_set = {needs} if isinstance(needs, str) else set(needs)
    assert "run" in needs_set
    assert collector_job["if"] == "${{ always() }}"
    assert collector_job["permissions"]["actions"] == "read"
    # The separate trusted collector can now publish recovery drafts and retire
    # incorporated refs. Its checkout remains main and never executes model code.
    assert collector_job["permissions"]["contents"] == "write"
    assert collector_job["permissions"]["pull-requests"] == "write"
    checkout = next(step for step in own_steps(collector_job)
                    if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["persist-credentials"] is False
    assert "factory_checkpoint_handoff.py" in collector
    # Issue #1093 is retired: the collector never patches a registry marker.
    assert "ATTEMPT_COMMENT_ID" not in collector
    assert "issues/comments" not in collector
    assert "run / factory" in collector


def test_recovery_watchdog_uses_trusted_bounded_store_runtime_view() -> None:
    workflow = adopter_view("fixed-model-factory-dispatch-recovery.yml")
    assert "from factory_runtime_view import export_view" in workflow
    assert "--view /tmp/factory-runtime-view.json" in workflow
    assert f"uses: {STORE_STEP}" in workflow
    # Issue #1093 is retired: freshness is store-only, with no comment fallback.
    assert "factory_registry_ingest.py" not in workflow
    assert "registry_comments()" not in workflow
    assert "comments" not in workflow
    health_first = workflow.index("refresh_health")
    fresh = workflow.index("An active factory heartbeat is fresh; no dispatcher recovery needed.")
    dispatch = workflow.index("gh workflow run fixed-model-factory-dispatch.yml")
    assert health_first < fresh < dispatch


def test_regular_status_job_prunes_only_in_trusted_controller():
    workflow = adopter_view("factory-status-pages.yml")
    assert "cron: '*/15 * * * *'" in workflow
    # A cleanup implementation merge must kick off the trusted status job.
    # Scheduled Actions can be delayed under heavy repository load.
    push_paths = workflow.split("  push:", 1)[1].split("permissions:", 1)[0]
    assert "'.github/workflows/factory-status-pages.yml'" in push_paths
    prune = "factory_state_operations.py prune"
    assert "Prune aged lifecycle events and roll up old model attempts" in workflow
    assert workflow.index(f"uses: {STORE_STEP}") < workflow.index(prune)
    assert workflow.index(prune) < workflow.index("Generate factory dashboard")
    # Cleanup no longer depends on a large scheduled PR-history recovery sweep.
    reconciliation = adopter_view("factory-issue-pr-state-reconciler.yml")
    assert "factory_state_operations.py prune" not in reconciliation


def test_factory_dashboard_reuses_one_run_local_inventory():
    workflow = adopter_view("factory-status-pages.yml")
    dashboard = workflow.split("      - name: Generate factory dashboard", 1)[1].split("      - name: Publish live dashboard snapshot", 1)[0]
    assert "FACTORY_DISCOVERY_DIR: ${{ runner.temp }}/factory-discovery" in dashboard
    assert "python3 .github/scripts/generate_factory_status_dashboard.py" in dashboard
    # Run-local discovery prevents duplicate all-issues/all-PRs enumerations
    # within one render. It does not grant cross-run authority.
    assert "FACTORY_DISCOVERY_DIR" not in workflow.split("      - name: Configure the factory store", 1)[0]


def test_runtime_worker_never_reads_or_writes_registry_comments() -> None:
    workflow = adopter_view("free-model-factory-run.yml")
    # Issue #1093 is retired as a registry: no locator, lookup or write remains.
    assert "issues/1093" not in workflow
    assert "issues/comments" not in workflow
    assert "FACTORY_HEARTBEAT_COMMENT_ID" not in workflow
    assert "FACTORY_ATTEMPT_COMMENT_ID" not in workflow
    assert "__heartbeat_id" not in workflow and "__attempt_id" not in workflow
    assert "factory_registry_verify.py" not in workflow
    # Lifecycle evidence goes to the trusted store from the token-bearing jobs only.
    assert workflow.count("factory_state_ledger.py lifecycle") == 3


def test_week_scanning_exports_run_at_most_hourly() -> None:
    status = adopter_view("factory-status-pages.yml")
    export = status.split("- name: Export provider and model reliability counts", 1)[1]
    export = export.split("- name:", 1)[0]
    assert "< 3300" in export and ".counts_generated_at" in export
    assert export.index("exit 0") < export.index("export-reliability")
    recovery = yaml.safe_load((CALLERS / "fixed-model-factory-dispatch-recovery.yml").read_text())
    on = recovery.get(True) or recovery["on"]
    assert [entry["cron"] for entry in on["schedule"]] == ["37 * * * *"]
