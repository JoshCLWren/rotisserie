"""One immutable store event per attempted model, not per worker run."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github/scripts"
sys.path.insert(0, str(SCRIPTS))

from factory_model_attempt_events import model_attempt_events  # noqa: E402
import factory_state_ledger as ledger  # noqa: E402
import factory_outcome_reconcile as reconcile  # noqa: E402

WORKER = "1004230852001"
RUN = "44908459888"
OUTCOME = {
    "worker": WORKER,
    "source": "nvidia",
    "model": "nvidia/nemotron-3-ultra-550b-a55b",
    "outcome_class": "work_failure",
    "exact_model_proven": True,
    "selected_kind": "pr",
    "selected_number": "5628",
    "persisted_work": False,
}


def line(stamp: str, source: str, model: str, message: str, worker: str = WORKER) -> str:
    return f"{stamp} [factory:{worker}][source:{source}][model:{model}] {message}\n"


LOG = (
    line(
        "2026-11-21T13:22:30.6918621Z",
        "openrouter-free",
        "recursive/flint-v1.0:free",
        "FACTORY_EXECUTION_CANDIDATE: source=openrouter-free model=recursive/flint-v1.0:free runtime=openrouter/recursive/flint-v1.0:free",
    )
    + line(
        "2026-11-21T13:24:10.4236097Z",
        "openrouter-free",
        "recursive/flint-v1.0:free",
        "agent exit status 1 for pr #5628",
    )
    + line(
        "2026-11-21T13:24:10.4618372Z",
        "openrouter-free",
        "recursive/flint-v1.0:free",
        "FACTORY_TRUSTED_FAILOVER: source=openrouter-free model=recursive/flint-v1.0:free outcome=provider_quota scope=model",
    )
    + line(
        "2026-11-21T13:24:14.2267751Z",
        "openrouter-free",
        "google/gemma-4-26b-a4b-it:free",
        "FACTORY_EXECUTION_CANDIDATE: source=openrouter-free model=google/gemma-4-26b-a4b-it:free runtime=openrouter/google/gemma-4-26b-a4b-it:free",
    )
    + line(
        "2026-11-21T13:25:54.9671618Z",
        "openrouter-free",
        "google/gemma-4-26b-a4b-it:free",
        "agent exit status 1 for pr #5628",
    )
    + line(
        "2026-11-21T13:25:54.9885797Z",
        "openrouter-free",
        "google/gemma-4-26b-a4b-it:free",
        "FACTORY_TRUSTED_FAILOVER: source=openrouter-free model=google/gemma-4-26b-a4b-it:free outcome=provider_quota scope=model",
    )
    + line(
        "2026-11-21T13:27:39.3961676Z",
        "nvidia",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "FACTORY_EXECUTION_CANDIDATE: source=nvidia model=nvidia/nemotron-3-ultra-550b-a55b runtime=nvidia/nvidia/nemotron-3-ultra-550b-a55b",
    )
    + line(
        "2026-11-21T13:32:27.5135129Z",
        "nvidia",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "agent exit status 0 for pr #5628",
    )
    + line(
        "2026-11-21T13:34:40.4202541Z",
        "nvidia",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "semantic_review_recovery_failed: no authoritative verdict",
    )
)


def test_fleet_switches_create_distinct_ordered_attempt_events() -> None:
    events = model_attempt_events(RUN, OUTCOME, LOG)
    assert len(events) == 3
    assert [e["event_id"] for e in events] == [
        f"model-attempt:{RUN}:1",
        f"model-attempt:{RUN}:2",
        f"model-attempt:{RUN}:3",
    ]
    assert [e["source"] for e in events] == ["openrouter-free", "openrouter-free", "nvidia"]
    assert [e["outcome_class"] for e in events] == [
        "provider_quota",
        "provider_quota",
        "work_failure",
    ]
    assert all(e["worker"] == WORKER and e["selected_number"] == "5628" for e in events)
    assert events[0]["agent_exit_code"] == 1
    assert events[-1]["agent_exit_code"] == 0
    assert events[0]["duration_ms"] is not None and events[0]["duration_ms"] > 90_000
    assert events[-1]["finished_at"] == "2026-11-21T13:32:27.5135129Z"
    assert events[-1]["evidence"] == "trusted_terminal"


def test_unfinished_candidate_remains_unknown_without_a_terminal_attestation() -> None:
    outcome = {**OUTCOME, "model": "different-model", "outcome_class": "success"}
    events = model_attempt_events(RUN, outcome, LOG)
    assert events[-1]["outcome_class"] == "unknown"
    assert events[-1]["evidence"] == "agent_exit"
    assert events[-1]["duration_ms"] is not None
    assert events[0]["outcome_class"] == "provider_quota"


def test_ansi_and_other_workers_cannot_create_extra_events() -> None:
    injected = line(
        "2026-11-21T13:24:12Z",
        "opencode-free",
        "fake",
        "FACTORY_EXECUTION_CANDIDATE: source=opencode-free model=fake runtime=opencode/fake",
        worker="999999",
    )
    bad = line(
        "2026-11-21T13:24:12Z",
        "opencode-free",
        "fake",
        "FACTORY_EXECUTION_CANDIDATE: source=nvidia model=fake runtime=nvidia/fake",
    )
    colored = LOG.replace("[factory:", "\x1b[32m[factory:").replace(
        "] executing", "]\x1b[0m executing"
    )
    events = model_attempt_events(RUN, OUTCOME, colored + injected + bad)
    assert len(events) == 3
    assert all(e["model"] != "fake" for e in events)


def test_repeated_same_model_is_separate_attempt_and_no_change_is_not_success() -> None:
    source, model = "opencode-free", "hy4-flash-free"
    start = line(
        "2026-11-21T13:00:00Z",
        source,
        model,
        f"FACTORY_EXECUTION_CANDIDATE: source={source} model={model} runtime=opencode/{model}",
    )
    no_change = line(
        "2026-11-21T13:00:30Z", source, model, "FACTORY_NO_CHANGE_FAILOVER: no changes"
    )
    retry = line(
        "2026-11-21T13:00:40Z",
        source,
        model,
        f"FACTORY_EXECUTION_CANDIDATE: source={source} model={model} runtime=opencode/{model}",
    )
    outcome = {**OUTCOME, "source": source, "model": model, "outcome_class": "success"}
    events = model_attempt_events(RUN, outcome, start + no_change + retry)
    assert [e["outcome_class"] for e in events] == ["no_progress", "success"]
    assert [e["attempt_index"] for e in events] == [1, 2]
    assert events[0]["duration_ms"] == 30_000
    assert events[1]["finished_at"] is None


def test_no_log_candidates_means_no_invented_attempts() -> None:
    assert model_attempt_events(RUN, OUTCOME, "No model selected\n") == []
    assert model_attempt_events(RUN, OUTCOME, "FACTORY_EXECUTION_CANDIDATE from agent prose") == []


def test_malformed_identity_and_unbounded_attempts_are_rejected() -> None:
    with pytest.raises(ValueError, match="identity"):
        model_attempt_events(RUN, {**OUTCOME, "worker": "1;DROP"}, LOG)
    with pytest.raises(ValueError, match="count"):
        model_attempt_events(RUN, OUTCOME, LOG.splitlines(keepends=True)[0] * 2001)


def test_terminal_and_each_attempt_share_one_trusted_recording(tmp_path, monkeypatch) -> None:
    import importlib

    import factory_turso_events as events

    from tests import turso_support as support

    batches: list[list[dict]] = []
    original = events.record_events

    def recording(repo: str, entries: list[dict]) -> int:
        batches.append(entries)
        return original(repo, entries)

    monkeypatch.setattr(events, "record_events", recording)
    outcome = {
        **OUTCOME,
        "source": importlib.import_module("factory_turso_operations").roster_source(WORKER),
    }
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        count = ledger.record_runtime_outcome(repo, RUN, outcome, job_log=LOG)
        stored = db.read(
            "SELECT event_type FROM factory_state_events WHERE repo = :r ORDER BY id", {"r": repo}
        )
    assert count == 4
    assert len(batches) == 1
    assert batches[0][0]["event_id"] == f"runtime:{RUN}"
    assert [e["event_type"] for e in batches[0][1:]] == ["model_attempt"] * 3
    assert [r["event_type"] for r in stored] == ["worker_failed"] + ["model_attempt"] * 3


def test_deferred_collector_uses_same_attempt_recording(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        ledger,
        "record_runtime_outcome",
        lambda repo, run, outcome, job_log=None: calls.append(job_log) or 4,
    )
    reconcile._record("owner/repo", RUN, dict(OUTCOME), LOG)
    assert calls == [LOG]


def test_failure_tail_preserves_model_specific_diagnostics_without_next_model_leakage() -> None:
    failing = (
        line(
            "2026-11-21T13:22:50Z",
            "openrouter-free",
            "recursive/flint-v1.0:free",
            "retrying (attempt 2): Rate limit exceeded: free-models-per-day-high-balance.",
        )
        + "2026-11-21T13:24:10.3315859Z \x1b[91mError: \x1b[0m"
        "Rate limit exceeded: free-models-per-day-high-balance.\n"
    )
    next_model = line(
        "2026-11-21T13:30:00Z",
        "nvidia",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "Error: independent nvidia review failure",
    )
    events = model_attempt_events(
        RUN,
        OUTCOME,
        LOG.replace(
            line(
                "2026-11-21T13:24:10.4236097Z",
                "openrouter-free",
                "recursive/flint-v1.0:free",
                "agent exit status 1 for pr #5628",
            ),
            failing
            + line(
                "2026-11-21T13:24:10.4236097Z",
                "openrouter-free",
                "recursive/flint-v1.0:free",
                "agent exit status 1 for pr #5628",
            ),
        )
        + next_model,
    )
    tail = events[0]["failure_log_tail"]
    assert "Rate limit exceeded: free-models-per-day-high-balance" in tail
    assert "agent exit status 1" in tail
    assert "FACTORY_TRUSTED_FAILOVER" in tail
    assert "independent nvidia review failure" not in tail
    assert "semantic_review_recovery_failed" in events[2]["failure_log_tail"]


def test_failure_tail_drops_secret_context_and_redacts_private_urls() -> None:
    source, model = "opencode-free", "hy4-flash-free"
    start = line(
        "2026-11-21T13:00:00Z",
        source,
        model,
        f"FACTORY_EXECUTION_CANDIDATE: source={source} model={model} runtime=opencode/{model}",
    )
    risky = (
        "2026-11-21T13:00:01Z Error: authorization Bearer sk-VERY-PRIVATE-CREDENTIAL\n"
        "2026-11-21T13:00:02Z Error: password=something-private\n"
        "2026-11-21T13:00:03Z Error: called https://host.example/private?code=VERYPRIVATESECRET\n"
        "2026-11-21T13:00:04Z Error: sent to nobody@example.com\n"
        "2026-11-21T13:00:05Z Error: request ID 0123456789abcdef0123456789abcdef0123 failed\n"
        "2026-11-21T13:00:06Z Python prompt with confidential source content\n"
    )
    fail = line(
        "2026-11-21T13:00:08Z",
        source,
        model,
        f"FACTORY_TRUSTED_FAILOVER: source={source} model={model} outcome=provider_quota scope=model",
    )
    event = model_attempt_events(
        RUN, {**OUTCOME, "source": source, "model": model}, start + risky + fail
    )[0]
    tail = event["failure_log_tail"]
    assert tail
    assert "authorization" not in tail.lower()
    assert "sk-VERY" not in tail
    assert "password" not in tail.lower()
    assert "https://host.example" not in tail
    assert "nobody@example.com" not in tail
    assert "0123456789abcdef" not in tail
    assert "confidential source content" not in tail
    assert "[redacted]" in tail
    assert "FACTORY_TRUSTED_FAILOVER" in tail


def test_failure_tail_is_bounded_to_last_diagnostic_lines() -> None:
    source, model = "opencode-free", "hy4-flash-free"
    start = line(
        "2026-11-21T13:00:00Z",
        source,
        model,
        f"FACTORY_EXECUTION_CANDIDATE: source={source} model={model} runtime=opencode/{model}",
    )
    errors = "".join(
        line("2026-11-21T13:00:01Z", source, model, f"Error: retry cycle {i}: " + "x" * 600)
        for i in range(50)
    )
    event = model_attempt_events(
        RUN, {**OUTCOME, "source": source, "model": model}, start + errors
    )[0]
    tail = event["failure_log_tail"]
    assert tail
    assert len(tail) <= 1600
    assert len(tail.splitlines()) <= 8
    assert "cycle 49" in tail
    assert "cycle 0" not in tail


def test_successful_final_attempt_has_no_failure_tail() -> None:
    outcome = {**OUTCOME, "outcome_class": "success"}
    events = model_attempt_events(RUN, outcome, LOG)
    assert events[-1]["outcome_class"] == "success"
    assert events[-1]["failure_log_tail"] is None
    assert events[0]["failure_log_tail"] is not None
