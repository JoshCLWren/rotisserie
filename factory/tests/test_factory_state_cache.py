"""Regression tests for the factory state connection and its cleanup (no network)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.workflow_files import adopter_view, own_steps

MODULE_PATH = Path(__file__).resolve().parents[1] / ".github/scripts/factory_state_cache.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("factory_state_cache_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


def test_neon_dsn_preserves_tls_and_removes_unsupported_channel_binding(monkeypatch, tmp_path):
    dsn = tmp_path / "dsn"
    dsn.write_text("postgresql://factory_state_rw:example@ep.example.neon.tech/factory_state?sslmode=require&channel_binding=require")
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", str(dsn))
    parsed = cache._dsn()
    assert "sslmode=require" in parsed
    assert "channel_binding" not in parsed
    assert "/factory_state?" in parsed


def test_workflows_inject_database_only_into_trusted_jobs():
    root = Path(__file__).resolve().parents[1]
    action = (root / ".github/actions/factory-state-cache/action.yml").read_text()
    assert "database_name=factory_state&role_name=factory_state_rw" in action
    assert '::add-mask::$uri' in action
    assert 'FACTORY_STATE_DSN_FILE=$dsn_file' in action
    for filename in (
        "fixed-model-factory-dispatch.yml",
        "factory-status-pages.yml",
        "fixed-model-factory-dispatch-recovery.yml",
    ):
        workflow = (root.parent / ".github/workflows" / filename).read_text()
        assert "uses: ./.github/actions/factory-state-cache" in workflow
        assert "secrets.NEON_API_KEY" in workflow
    # The local independent reviewer must never receive database credentials.
    local_review = adopter_view("local-ollama-review.yml")
    assert "factory-state-cache" not in local_review
    assert "FACTORY_STATE_DSN_FILE" not in local_review

    # In the reusable workflow only the separate trusted cleanup runner can
    # connect to Neon, not the agent/model execution job.
    import yaml

    reusable = yaml.safe_load(
        adopter_view("free-model-factory-run.yml")
    )
    model_job = reusable["jobs"]["factory"]
    cleanup_job = reusable["jobs"]["release_pre_session_lease"]
    completed_cleanup_job = reusable["jobs"]["release_post_session_lease"]
    assert "factory-state-cache" not in str(model_job)
    assert "FACTORY_STATE_DSN_FILE" not in str(model_job)
    assert "NEON_API_KEY" not in str(model_job)
    for trusted_job in (cleanup_job, completed_cleanup_job):
        assert any(
            step.get("uses") == "./.github/actions/factory-state-cache"
            for step in own_steps(trusted_job)
        )

    # The trusted Entry snapshot job reads Neon; the reusable worker never does.
    entry = adopter_view("free-model-factory-entry.yml")
    snapshot, collector = entry.split("\n  persist_outcome:", 1)
    preparation = reusable["jobs"]["validate_assignment"]
    assert model_job["needs"] == "validate_assignment"
    assert any(step.get("uses") == "./.github/actions/factory-state-cache"
               for step in own_steps(preparation))
    assert model_job["env"]["FACTORY_MODEL_EXCLUSIONS"] == "${{ needs.validate_assignment.outputs.exclusions }}"
    assert "FACTORY_STATE_DSN_FILE" not in snapshot
    assert "factory-state-cache" in collector
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


def test_recovery_watchdog_uses_trusted_bounded_neon_runtime_view() -> None:
    workflow = adopter_view("fixed-model-factory-dispatch-recovery.yml")
    assert "from factory_runtime_view import export_view" in workflow
    assert "--view /tmp/factory-runtime-view.json" in workflow
    assert "uses: ./.github/actions/factory-state-cache" in workflow
    # Issue #1093 is retired: freshness is Neon-only, with no comment fallback.
    assert "factory_registry_ingest.py" not in workflow
    assert "registry_comments()" not in workflow
    assert "comments" not in workflow
    health_first = workflow.index("refresh_health")
    fresh = workflow.index("An active factory heartbeat is fresh; no dispatcher recovery needed.")
    dispatch = workflow.index("gh workflow run fixed-model-factory-dispatch.yml")
    assert health_first < fresh < dispatch


def test_event_pruning_retains_recent_worker_lifecycle_and_coordination(monkeypatch):
    executions = []
    def fake_run(statement, *args, one=False):
        executions.append((statement, args, one))
        return {"deleted": 17}
    monkeypatch.setattr(cache, "_run", fake_run)
    assert cache._prune_retired_events("acme/widgets", 500) == 17
    sql, args, one = executions[0]
    assert one is True
    assert args == (
        "factory-github-get-v1:acme/widgets:",
        "factory-projection-v1:acme/widgets:",
        ["acme/widgets:open-issues", "acme/widgets:open-prs"],
        "acme/widgets", 500,
    )
    assert "event_type='cache_refresh'" in sql
    assert "event_type='target_reconciled'" in sql
    assert "created_at < now() - interval '30 days'" in sql
    assert "detail->>'repo'=$4 OR detail->>'repository'=$4" in sql
    assert "event_type IN ('cache_refresh'," in sql
    # Lifecycle history is removable only after 30 days AND explicit repo
    # provenance. Unscoped historical events are never guessed or deleted.
    assert "event_type IN ('cache_refresh'," in sql
    assert "'worker_assigned'" in sql
    assert "'worker_completed'" in sql
    assert "'worker_heartbeat'" in sql
    assert "'worker_failed'" in sql
    assert "detail->>'repo'=$4 OR detail->>'repository'=$4" in sql
    assert "DELETE FROM public.factory_state_events" in sql
    assert "factory_state_kv" not in sql
    assert "factory_provider_reservation" not in sql
    # No LIKE wildcards that could turn underscores in repository names into patterns.
    assert "LIKE" not in sql
    with pytest.raises(ValueError, match="batch"):
        cache._prune_retired_events("acme/widgets", 1001)
    with pytest.raises(ValueError, match="scope"):
        cache._prune_retired_events("other-repo", 100)


def test_worker_event_writers_tag_all_future_lifecycle_events_with_repo():
    ledger = "".join(path.read_text() for path in sorted(
        (Path(__file__).resolve().parents[1] / ".github/scripts").glob("factory_state_ledger*.py")))
    assert "'source', $6::text, 'repo', $7::text" in ledger
    assert "'targets',$5::jsonb,'repo',$6::text" in ledger
    assert 'json.dumps({**entry, "repo": repo})' in ledger
    # Repository scoping must be applied by the trusted collector, not
    # accepted from worker-supplied event payloads.
    assert 'json.dumps(entry)' not in ledger


def test_retired_check_projection_cleanup_is_scoped_and_bounded(monkeypatch):
    calls = []
    def fake_run(sql, *args, one=False):
        calls.append((sql, args, one))
        return {"deleted": 250}
    monkeypatch.setattr(cache, "_run", fake_run)
    # Exercise the actual validator. The previous raw-regex double escape
    # rejected every normal owner/repository and broke the live status job.
    assert cache._prune_retired_check_projection("acme/widgets") == 250
    assert cache._prune_retired_check_projection("other-owner/repo_name") == 250
    sql, args, one = calls[0]
    assert args == ("acme/widgets", 250)
    assert one is True
    assert "WHERE repo=$1" in sql
    assert "checks.repo=$1" in sql
    assert "LIMIT $2" in sql
    assert "DELETE FROM public.factory_check_projection" in sql
    for protected in ("factory_pr_stage_retry", "factory_provider_reservation",
                      "factory_state_events", "factory_state_kv"):
        assert protected not in sql
    with pytest.raises(ValueError, match="batch"):
        cache._prune_retired_check_projection("acme/widgets", 1001)
    with pytest.raises(ValueError, match="scope"):
        cache._prune_retired_check_projection("another-repo", 250)


def test_prune_is_bounded_to_retired_discovery_without_coordination(monkeypatch):
    executed = []
    def fake_run(statement, *args, one=False):
        executed.append((statement, args, one))
        return {"deleted": 4}
    monkeypatch.setattr(cache, "_run", fake_run)
    assert cache._prune_obsolete_discovery("acme/widgets", 200) == 4
    query, args, one = executed[0]
    assert args == ("factory-github-get-v1:acme/widgets:%",
                    "factory-projection-v1:acme/widgets:%",
                    ["acme/widgets:open-issues", "acme/widgets:open-prs"], 200)
    assert one is True
    assert "DELETE FROM public.factory_state_kv" in query
    assert "cache_key LIKE $1 OR cache_key LIKE $2" in query
    cache._prune_obsolete_discovery("acme/repo_name", 200)
    # An unescaped underscore would also match another repository's keys.
    assert executed[1][1][0] == "factory-github-get-v1:acme/repo\\_name:%"
    assert "LIMIT $4" in query
    assert "refresh_until IS NULL OR" in query
    with pytest.raises(ValueError, match="batch"):
        cache._prune_obsolete_discovery("acme/widgets", 1001)


def test_prune_cli_cleans_in_batches_with_upper_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", str(tmp_path / "connection"))
    calls = []
    def prune(_repo):
        calls.append(True)
        return 200 if len(calls) < 3 else 12
    monkeypatch.setattr(cache, "_prune_obsolete_discovery", prune)
    event_calls = []
    def prune_events(_repo):
        event_calls.append(True)
        return 1000 if len(event_calls) < 3 else 7
    monkeypatch.setattr(cache, "_prune_retired_events", prune_events)
    projection_calls = []
    def prune_checks(_repo):
        projection_calls.append(True)
        return 250 if len(projection_calls) < 4 else 7
    monkeypatch.setattr(cache, "_prune_retired_check_projection", prune_checks)
    import factory_state_retention
    monkeypatch.setattr(factory_state_retention, "prune_model_attempts", lambda _repo: 3)
    assert cache.main(["prune-obsolete"]) == 0
    assert len(calls) == 3
    assert len(event_calls) == 3
    assert len(projection_calls) == 4


def test_regular_status_job_prunes_only_in_trusted_controller():
    root = Path(__file__).resolve().parents[1]
    workflow = adopter_view("factory-status-pages.yml")
    assert "cron: '*/15 * * * *'" in workflow
    # A cleanup implementation merge must kick off the trusted status job.
    # Scheduled Actions can be delayed under heavy repository load.
    push_paths = workflow.split("  push:", 1)[1].split("permissions:", 1)[0]
    assert "'.github/workflows/factory-status-pages.yml'" in push_paths
    assert "Prune retired discovery and audit events" in workflow
    assert "factory_state_cache.py prune-obsolete" in workflow
    assert workflow.index("uses: ./.github/actions/factory-state-cache") < workflow.index("factory_state_cache.py prune-obsolete")
    assert workflow.index("factory_state_cache.py prune-obsolete") < workflow.index("Generate factory dashboard")
    # Cleanup no longer depends on a large scheduled PR-history recovery sweep.
    reconciliation = adopter_view("factory-issue-pr-state-reconciler.yml")
    assert "factory_state_cache.py prune-obsolete" not in reconciliation


def test_factory_dashboard_reuses_one_run_local_inventory():
    root = Path(__file__).resolve().parents[1]
    workflow = adopter_view("factory-status-pages.yml")
    dashboard = workflow.split("      - name: Generate factory dashboard", 1)[1].split("      - name: Publish live dashboard snapshot", 1)[0]
    assert "FACTORY_DISCOVERY_DIR: ${{ runner.temp }}/factory-discovery" in dashboard
    assert "python3 .github/scripts/generate_factory_status_dashboard.py" in dashboard
    # Run-local discovery prevents duplicate all-issues/all-PRs enumerations
    # within one render. It does not grant cross-run authority.
    assert "FACTORY_DISCOVERY_DIR" not in workflow.split("      - name: Configure shared Neon factory discovery cache", 1)[0]


def test_runtime_worker_never_reads_or_writes_registry_comments() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = adopter_view("free-model-factory-run.yml")
    # Issue #1093 is retired as a registry: no locator, lookup or write remains.
    assert "issues/1093" not in workflow
    assert "issues/comments" not in workflow
    assert "FACTORY_HEARTBEAT_COMMENT_ID" not in workflow
    assert "FACTORY_ATTEMPT_COMMENT_ID" not in workflow
    assert "__heartbeat_id" not in workflow and "__attempt_id" not in workflow
    assert "factory_registry_verify.py" not in workflow
    # Lifecycle evidence goes to trusted Neon from the DSN-bearing jobs only.
    assert workflow.count("factory_state_ledger.py lifecycle") == 3
