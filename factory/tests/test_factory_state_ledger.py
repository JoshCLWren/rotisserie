"""Trusted-only Neon lifecycle and transactional lease regressions (no network)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / ".github/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("factory_state_ledger_test", SCRIPTS / "factory_state_ledger.py")
assert SPEC is not None and SPEC.loader is not None
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)


def test_keys_cover_worker_and_entire_target_group():
    assert ledger._keys("owner/repo", "42", [501, 499, 501]) == [
        "factory-lease-v1:owner/repo:worker:42",
        "factory-lease-v1:owner/repo:target:499",
        "factory-lease-v1:owner/repo:target:501",
    ]


def test_unconfigured_controller_retains_legacy_github_claim_behavior(monkeypatch):
    monkeypatch.delenv("FACTORY_STATE_DSN_FILE", raising=False)
    assert ledger.acquire_lease("owner/repo", "42", [5001]) == ""
    assert ledger.release_lease("owner/repo", "42", "", "rollback") is False


@pytest.mark.parametrize("worker,numbers,ttl", [
    ("factory:42", [5001], 900),
    ("42", [], 900),
    ("42", [0], 900),
    ("42", [5001], 30),
])
def test_invalid_lease_requests_rejected(worker, numbers, ttl):
    with pytest.raises(ValueError):
        ledger.acquire_lease("owner/repo", worker, numbers, ttl)


def test_claim_conflict_propagates_without_github_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", str(tmp_path / "dsn"))
    monkeypatch.setattr(ledger, "_acquire", lambda *args: "unused")
    monkeypatch.setattr(ledger.asyncio, "run", lambda coroutine: None)
    assert ledger.acquire_lease("owner/repo", "42", [5001]) is None


def test_db_error_is_not_silenced_after_configured_claim(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", str(tmp_path / "dsn"))
    def fail(coroutine):
        coroutine.close()
        raise ConnectionError("Neon down")
    monkeypatch.setattr(ledger.asyncio, "run", fail)
    with pytest.raises(ConnectionError):
        ledger.acquire_lease("owner/repo", "42", [5001])


def test_trusted_lifecycle_start_is_recorded_once_per_run(monkeypatch):
    found = []
    monkeypatch.setattr(ledger, "record_events", lambda repo, entries: found.extend(entries) or len(entries))
    assert ledger.record_lifecycle("owner/repo", "43123", "42", "started") == 1
    assert found[0]["event_type"] == "worker_started"
    assert found[0]["worker"] == "42"
    assert found[0]["run_id"] == "43123"
    # Idempotency is per run/status in _events, never per comment edit.
    assert found[0]["event_id"] == "lifecycle:43123:started"
    assert not hasattr(ledger, "record_heartbeat_comments")


def test_terminal_classification_is_idempotency_keyed_by_run(monkeypatch):
    records = []
    monkeypatch.setattr(ledger, "record_events", lambda _, events: records.extend(events) or len(events))
    assert ledger.record_runtime_outcome("owner/repo", "12345", {
        "worker": "42", "outcome_class": "provider_failure",
        "persisted_work": False,
    }) == 1
    assert records[0]["event_type"] == "worker_failed"
    assert records[0]["event_id"] == "runtime:12345"
    records.clear()
    ledger.record_runtime_outcome("owner/repo", "12346", {
        "worker": "42", "outcome_class": "success",
        "persisted_work": True,
    })
    assert records[0]["event_type"] == "worker_completed"
    assert records[0]["persisted_work"] is True


def test_idle_is_not_a_successful_delivery_or_failed_worker(monkeypatch):
    records = []
    monkeypatch.setattr(ledger, "record_events", lambda _, events: records.extend(events) or len(events))
    ledger.record_runtime_outcome("owner/repo", "12347", {
        "worker": "42", "outcome_class": "no_work",
        "persisted_work": False, "delivery_outcome": "none",
    })
    assert records[0]["event_type"] == "worker_idle"
    assert records[0]["delivery_outcome"] == "none"



def test_timed_out_agent_with_verified_delivery_is_completed(monkeypatch):
    """Worker 82: a real code handoff is not erased by its agent timeout."""
    records = []
    monkeypatch.setattr(ledger, "record_events", lambda _, events: records.extend(events) or len(events))
    ledger.record_runtime_outcome("owner/repo", "43639844249", {
        "worker": "82", "outcome_class": "agent_timeout",
        "persisted_work": True, "delivery_outcome": "persisted",
        "delivery_pr": "5129", "selected_kind": "issue", "selected_number": "4839",
    })
    assert records[0]["event_type"] == "worker_completed"
    assert records[0]["outcome_class"] == "agent_timeout"
    assert records[0]["delivery_outcome"] == "persisted"
    assert records[0]["delivery_pr"] == "5129"


def test_timed_out_agent_without_delivery_stays_failed(monkeypatch):
    """Worker 105: lease cleanup alone is not a successful code delivery."""
    records = []
    monkeypatch.setattr(ledger, "record_events", lambda _, events: records.extend(events) or len(events))
    ledger.record_runtime_outcome("owner/repo", "43640000000", {
        "worker": "105", "outcome_class": "agent_timeout",
        "persisted_work": False, "delivery_outcome": "none",
    })
    assert records[0]["event_type"] == "worker_failed"
    assert records[0]["delivery_outcome"] == "none"


def test_full_session_budget_without_agent_fifty_minute_cap():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/free-model-factory-run.yml").read_text())
    assert int(workflow["env"]["FACTORY_BUDGET_SECONDS"]) == 6000
    assert workflow["jobs"]["factory"]["timeout-minutes"] == 120
    for name in (
        "free-model-factory-worker.sh",
        "free-model-factory-worker-primitives.sh",
        "omniroute-factory-worker.sh",
        "nvidia-factory-worker.sh",
    ):
        assert "agent_timeout > 3000" not in (root / ".github/scripts" / name).read_text()
    main = (root / ".github/scripts/free-model-factory-worker.sh").read_text()
    assert "available - 240 - FAST_CHECK_RESERVE_SECONDS" in main


def test_trusted_collector_verifies_code_handoff_before_persistence():
    root = Path(__file__).resolve().parents[1]
    entry = (root / ".github/workflows/free-model-factory-entry.yml").read_text()
    assert 'delivery_pr="$(jq -r' in entry
    assert 'retry_gh_api "$RUNNER_TEMP/factory-delivery-pr.json"' in entry
    assert '"repos/$GITHUB_REPOSITORY/pulls/$delivery_pr"' in entry
    assert '.head.repo.full_name == $repo' in entry
    assert "pull-requests: write" in entry
    assert "factory_checkpoint_handoff.py" in entry
    # The verdict is Neon plus job summary; no #1093 marker is patched.
    assert "ATTEMPT_COMMENT_ID" not in entry
    assert "issues/comments" not in entry
    assert "Neon terminal classification" in entry
    observer = (root / ".github/workflows/fixed-model-runtime-observer.yml").read_text()
    assert 'gh api "repos/$GITHUB_REPOSITORY/pulls/$delivery_pr"' in observer
    assert "pull-requests: read" in observer
    assert "Verified delivery:" in observer
    assert "issues/comments" not in observer


def test_only_trusted_collectors_and_observer_use_ledger():
    root = Path(__file__).resolve().parents[1]
    controller = (root / ".github/scripts/factory-work-controller.py").read_text()
    collector = (root / ".github/scripts/factory_completion_controller.py").read_text()
    observer = (root / ".github/workflows/fixed-model-runtime-observer.yml").read_text()
    untrusted = (root / ".github/workflows/free-model-factory-run.yml").read_text()
    entry = (root / ".github/workflows/free-model-factory-entry.yml").read_text()
    assert "acquire_lease(REPO, worker, numbers, FIXED_LEASE_TTL_SECONDS)" in controller
    assert "release_lease(REPO, worker, claim_token, 'claim-rollback')" in controller
    # Registry heartbeat scraping was replaced by durable runtime projections
    # (#5238); the completion collector no longer writes the ledger itself.
    assert "record_heartbeat_comments" not in collector
    # Issue #1093 read-back was retired; trusted run jobs write lifecycle.
    assert not (root / ".github/scripts/factory_registry_ingest.py").exists()
    assert "factory_state_ledger.py lifecycle" in untrusted
    assert "Persist classified worker completion in Neon" in observer
    trusted_cleanup = yaml.safe_load(untrusted)["jobs"]["release_pre_session_lease"]
    model_job = yaml.safe_load(untrusted)["jobs"]["factory"]
    assert "factory-state-cache" not in str(model_job)
    assert "NEON_API_KEY" not in str(model_job)
    assert any(step.get("uses") == "./.github/actions/factory-state-cache"
               for step in trusted_cleanup["steps"])
    assert "validate_assignment:" in (root / ".github/workflows/free-model-factory-run.yml").read_text()
    assert "factory-state-cache" in entry.split("  persist_outcome:", 1)[1]


def test_trusted_heartbeat_events_extend_current_nonexpired_lease() -> None:
    import inspect

    source = inspect.getsource(ledger._events)
    assert 'entry["event_type"] == "worker_started"' in source
    assert "def renew_verified_workers(" in inspect.getsource(ledger.renew_verified_workers)
    assert "expires_at>now()" in inspect.getsource(ledger._renew_verified)
    assert "AND expires_at>now()" in source
    assert "payload->0->>'claim_id'=$2" in source


def test_completion_dispatch_persists_real_worker_outcomes() -> None:
    root = Path(__file__).resolve().parents[1]
    entry = (root / ".github/workflows/free-model-factory-entry.yml").read_text()
    drain = (root / ".github/workflows/factory-completion-drain.yml").read_text()
    assert "persist_outcome:" in entry
    assert "needs: run" in entry
    assert "if: ${{ always() }}" in entry
    assert "actions/jobs/${job_id}/logs" in entry
    assert "factory_state_ledger.py runtime-outcome" in entry
    assert "gh workflow run factory-completion-drain.yml" in entry
    assert "persist_outcome:" not in drain
    assert "source_run_id" in drain
    assert "factory-state-cache" not in drain
    # Model execution is confined to the reusable runner, which has no Neon
    # secret. The separate post-run collector receives the restricted DSN.
    assert "validate_assignment:" in (root / ".github/workflows/free-model-factory-run.yml").read_text()
    reusable = yaml.safe_load((root / ".github/workflows/free-model-factory-run.yml").read_text())
    assert "factory-state-cache" not in str(reusable["jobs"]["factory"])
    assert "NEON_API_KEY" not in str(reusable["jobs"]["factory"])


def test_verified_lease_renewal_is_only_called_after_actions_liveness() -> None:
    import inspect

    controller = (Path(__file__).resolve().parents[1] / ".github/scripts/factory-work-controller.py").read_text()
    assert "renew_verified_workers(REPO, [str(worker) for worker in active])" in controller
    assert "prior_claims[worker_id] = worker_claim(REPO, worker_id)" in controller
    assert "release_lease(REPO, worker_id, token, 'stale-lease-reconciled')" in controller
    assert "not target_owned_by(number, f'factory:{worker_id}')" in controller
    assert ledger.LIFECYCLE_STATUSES == {
        "started": "worker_started",
        "finished": "worker_outcome_signal",
        "failure": "worker_outcome_signal",
    }
    assert "record_events" in inspect.getsource(ledger.record_lifecycle)

def test_terminal_event_idempotency_spans_classification_outcomes() -> None:
    """A single source run cannot be counted as both success and failure."""
    import inspect

    source = inspect.getsource(ledger._events)
    assert "detail->>'event_id'=$1 LIMIT 1" in source
    assert "AND event_type=$1" not in source


def test_terminal_identity_fallback_uses_attested_run_title() -> None:
    """Actions run GET omits workflow_dispatch inputs for older Entry runs."""
    root = Path(__file__).resolve().parents[1]
    entry = (root / ".github/workflows/free-model-factory-entry.yml").read_text()
    assert "INPUT_WORKER: ${{ inputs.worker }}" in entry
    assert 'jq --arg worker "$INPUT_WORKER" ' in entry
    assert "run / factory" in entry
    assert "GITHUB_RUN_ID" in entry


def test_terminal_collector_can_download_ansi_colored_worker_logs() -> None:
    """The current gh CLI rejects raw job logs containing ANSI escape codes."""
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/free-model-factory-entry.yml").read_text()
    assert 'retry_gh_api "$RUNNER_TEMP/factory-entry.log" --allow-escape-sequences' in workflow
    assert '"repos/${GITHUB_REPOSITORY}/actions/jobs/${job_id}/logs"' in workflow
    assert 'if gh api "$@" > "$file" 2> "$error_file"; then' in workflow
    assert "factory_state_ledger.py runtime-outcome" in workflow
    # The opt-out is only safe for redirected file output, never terminal output.
    assert "--allow-escape-sequences" not in (
        root / ".github/workflows/free-model-factory-run.yml"
    ).read_text()


def test_completion_persistence_is_not_queued_behind_dispatch() -> None:
    """The only serialized entry signal is for capacity, never event writes."""
    root = Path(__file__).resolve().parents[1]
    entry = (root / ".github/workflows/free-model-factory-entry.yml").read_text()
    drain = (root / ".github/workflows/factory-completion-drain.yml").read_text()
    assert "  persist_outcome:" in entry
    assert "  signal_completion:" in entry
    collector = entry.split("  persist_outcome:", 1)[1]
    assert "concurrency:" not in collector
    assert "factory-state-cache" in collector
    assert 'if: ${{ always() }}' in collector
    assert "  persist_outcome:" not in drain
    assert "group: factory-completion-drain-ingress" in drain



@pytest.mark.parametrize("worker,model", [
    ("20", "openai/gpt-oss-20b"),
    ("21", "google/gemma-4-31b-it"),
])
def test_trusted_pinned_smoke_timeout_enforces_provider_backoff(worker, model):
    """Production-shaped 180s timeouts are not unclassified runner failures."""
    outcome = {"worker": worker, "source": "nvidia", "model": model,
               "outcome_class": "unknown_failure", "exact_model_proven": False,
               "persisted_work": False}
    log = (
        "2026-11-14T14:11:38Z ##[group]Run set -Eeuo pipefail\n"
        "2026-11-14T14:11:38Z \x1b[36;1mtimeout --signal=TERM --kill-after=10s 180s \\\x1b[0m\n"
        "2026-11-14T14:11:38Z RUNTIME_MODEL: nvidia/" + model + "\n"
        "2026-11-14T14:14:39Z ##[endgroup]\n"
        "2026-11-14T14:14:39Z ##[error]Process completed with exit code 124.\n"
    )
    adjusted = ledger.refine_trusted_smoke_timeout(outcome, log)
    assert adjusted["outcome_class"] == "provider_failure"
    assert "180-second smoke deadline" in adjusted["detail"]
    assert adjusted["source"] == outcome["source"]


def test_trusted_smoke_refinement_never_attributes_unrelated_timeout():
    outcome = {"source": "nvidia", "model": "known", "outcome_class": "unknown_failure",
               "exact_model_proven": False, "persisted_work": False}
    log = (
        "##[group]Run echo smoke command in docs\n"
        "timeout --signal=TERM --kill-after=10s 180s\n"
        "##[endgroup]\n"
        "##[group]Run checkout\n"
        "##[error]Process completed with exit code 124\n"
    )
    assert ledger.refine_trusted_smoke_timeout(outcome, log) == outcome
    assert ledger.refine_trusted_smoke_timeout(
        {**outcome, "exact_model_proven": True},
        "##[group]Run timeout --signal=TERM --kill-after=10s 180s\n"
        "##[error]Process completed with exit code 124\n",
    )["outcome_class"] == "unknown_failure"


def test_entry_passes_completed_job_log_into_trusted_ledger():
    root = Path(__file__).resolve().parents[1]
    entry = (root / ".github/workflows/free-model-factory-entry.yml").read_text()
    assert '--log "$RUNNER_TEMP/factory-entry.log"' in entry
    assert entry.index("factory_dynamic_outcome.py") < entry.index("factory_state_ledger.py runtime-outcome")



def test_worker_46_explicit_agent_timeout_is_not_unknown_failure():
    """Live run 43602063085 had a pinned-model 124 and transient interruption."""
    outcome = {
        "worker": "46", "source": "kilo-auto", "model": "kilo-auto/free",
        "outcome_class": "unknown_failure", "exact_model_proven": False,
        "persisted_work": False,
    }
    log = (
        "2026-11-14T14:06:47Z [factory:46][source:kilo-auto]"
        "[model:kilo-auto/free] agent exit status 124 for pr #5053\n"
        "2026-11-14T14:06:47Z [factory:46][source:kilo-auto]"
        "[model:kilo-auto/free] transient provider/runtime interruption "
        "on the pinned model; refusing to switch models\n"
    )
    refined = ledger.refine_trusted_smoke_timeout(outcome, log)
    assert refined["outcome_class"] == "agent_timeout"
    assert refined["source"] == "kilo-auto"
    assert refined["persisted_work"] is False
    assert ledger.refine_trusted_smoke_timeout(
        {**outcome, "worker": "47"}, log
    )["outcome_class"] == "unknown_failure"
    assert ledger.refine_trusted_smoke_timeout(
        {**outcome, "persisted_work": True}, log
    )["outcome_class"] == "unknown_failure"


def test_local_agent_timeout_does_not_require_provider_interruption_claim():
    outcome = {
        "worker": "46", "source": "kilo-auto", "model": "kilo-auto/free",
        "outcome_class": "unknown_failure", "exact_model_proven": False,
        "persisted_work": False,
    }
    log = "2026-11-14T14:06:47Z [factory:46] agent exit status 124 for pr #5053\n"
    assert ledger.refine_trusted_smoke_timeout(outcome, log)["outcome_class"] == "agent_timeout"


def test_trusted_deadline_overrides_incidental_provider_throttle_and_controller_text():
    outcome = {
        "worker": "82", "source": "z-ai", "model": "glm-4.7-flash",
        "outcome_class": "provider_throttle", "exact_model_proven": True,
        "persisted_work": False,
    }
    log = (
        "2026-11-14T18:47:11Z [factory:82][source:z-ai][model:glm-4.7-flash] "
        "agent exit status 124 for issue #4841\n"
    )
    assert ledger.refine_trusted_smoke_timeout(outcome, log)["outcome_class"] == "agent_timeout"
    quota = {**outcome, "outcome_class": "provider_quota"}
    assert ledger.refine_trusted_smoke_timeout(quota, log) == quota


def test_trusted_ansi_deprecated_smoke_attributes_actual_rotated_selector():
    """Worker 111 selected GLM, then tried deprecated Crouton at run 43610131794."""
    outcome = {
        "worker": "111", "source": "opencode-free", "model": "glm-5.3-flash-free",
        "outcome_class": "unknown_failure", "exact_model_proven": False,
        "persisted_work": False,
    }
    log = (
        "2026-11-14T15:26:50Z ##[group]Run set -Eeuo pipefail\n"
        "2026-11-14T15:26:52Z Smoke candidate: opencode/crouton-alpha-free\n"
        "2026-11-14T15:26:53Z \x1b[91m\x1b[1mError: \x1b[0mModel crouton-alpha-free has been deprecated.\n"
        "2026-11-14T15:26:53Z ##[error]Process completed with exit code 1.\n"
    )
    refined = ledger.refine_trusted_smoke_timeout(outcome, log)
    assert refined["outcome_class"] == "model_unavailable"
    assert refined["model"] == "crouton-alpha-free"
    assert ledger.refine_trusted_smoke_timeout({**outcome, "persisted_work": True}, log) == {
        **outcome, "persisted_work": True,
    }
    assert ledger.refine_trusted_smoke_timeout(outcome, log.replace(
        "crouton-alpha-free has been deprecated", "unrelated-free has been deprecated",
    )) == outcome


def test_trusted_nvidia_410_records_sticky_retirement():
    outcome = {
        "source": "nvidia", "model": "retired-model", "outcome_class": "model_unavailable",
        "exact_model_proven": False, "persisted_work": False,
    }
    log = "2026-11-14T15:10:23Z NVIDIA permanently retired model: retired-model\n"
    assert ledger.refine_trusted_smoke_timeout(outcome, log)["outcome_class"] == "model_retired_410"
    assert ledger.refine_trusted_smoke_timeout(
        {**outcome, "model": "unrelated-model"}, log,
    )["outcome_class"] == "model_unavailable"


def test_successful_dynamic_rotation_persists_failed_models_and_real_final_model():
    outcome = {
        "worker": "111", "source": "opencode-free", "model": "glm-5.3-flash-free",
        "exact_model_proven": True, "persisted_work": True, "outcome_class": "success",
    }
    log = (
        "2026-11-14T15:26:50Z ##[group]Run set -Eeuo pipefail\n"
        "2026-11-14T15:26:52Z Smoke candidate: opencode/crouton-alpha-free\n"
        "2026-11-14T15:26:53Z \x1b[91mError: \x1b[0mModel crouton-alpha-free has been deprecated.\n"
        "2026-11-14T15:26:55Z Smoke candidate: opencode/big-pickle\n"
        "2026-11-14T15:26:56Z Dynamic free-model smoke succeeded: opencode/big-pickle\n"
    )
    refined = ledger.refine_trusted_dynamic_models(outcome, log)
    assert refined["model"] == "big-pickle"
    assert refined["model_failures"] == ["crouton-alpha-free"]
    assert ledger.refine_trusted_dynamic_models(outcome,
        log.replace("crouton-alpha-free has been deprecated", "other-free has been deprecated"),
    ).get("model_failures") is None
    assert ledger.refine_trusted_dynamic_models({**outcome, "source": "nvidia"}, log) == {**outcome, "source": "nvidia"}


def test_trusted_terminal_collector_retries_scoped_reads_and_idempotent_writes():
    """A temporary API or Neon outage must not lose a completed worker outcome."""
    root = SCRIPTS.parent.parent
    entry = (root / ".github/workflows/free-model-factory-entry.yml").read_text()
    assert "retry_gh_api()" in entry
    assert 'if gh api "$@" > "$file" 2> "$error_file"; then' in entry
    assert 'for attempt in 1 2 3 4; do' in entry
    assert 'retry_gh_api "$RUNNER_TEMP/factory-run-jobs.json"' in entry
    assert 'retry_gh_api "$RUNNER_TEMP/factory-entry.log"' in entry
    assert 'retry_gh_api "$RUNNER_TEMP/factory-delivery-pr.json"' in entry
    assert "if python3 .github/scripts/factory_state_ledger.py runtime-outcome" in entry
    assert 'Unable to persist Neon terminal outcome after 4 attempts' in entry
    # Markdown backticks must be quoted so shell does not execute agent_timeout.
    assert "printf 'Neon terminal classification: `%s`\\n'" in entry
    assert 'echo "Neon terminal classification: `' not in entry
    operations = (root / ".github/scripts/factory_state_operations.py").read_text()
    assert "INSERT INTO public.factory_runtime_event_keys(repo,run_id)" in operations
    assert "ON CONFLICT DO NOTHING RETURNING run_id" in operations
