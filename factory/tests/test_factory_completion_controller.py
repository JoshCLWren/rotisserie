"""Regression coverage for completion-drain scheduling and worker health."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "factory_completion_controller.py"
CONTROLLER_PATH = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "factory-work-controller.py"
# The controller lazily imports sibling modules (factory_worker_slots, ...).
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("factory_completion_controller", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
controller = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = controller
SPEC.loader.exec_module(controller)


def test_completion_claims_stop_at_omniroute_free_entry_cap():
    """The dispatcher (assign) is the only assignment-capable path; completion
    drain signals it and stops at the OmniRoute free-entry cap before assigning."""
    controller_path = (
        Path(__file__).resolve().parents[1] / ".github" / "scripts" / "factory-work-controller.py"
    )
    text = controller_path.read_text(encoding="utf-8")
    assert "omniroute_free_entry_has_capacity" in text
    assert "OmniRoute free-entry cap reached" in text
    # The cap check precedes any assignment in the dispatcher lane.
    assert text.index("omniroute_free_entry_has_capacity") < text.index(
        "def assign(worker"
    )


def test_completion_batch_size_scales_with_backlog():
    assert controller.completion_batch_size(2) == 0
    assert controller.completion_batch_size(3) == 8
    assert controller.completion_batch_size(7) == 8
    assert controller.completion_batch_size(8) == 8
    assert controller.completion_batch_size(19) == 8
    assert controller.completion_batch_size(20) == 12
    assert controller.completion_batch_size(80) == 12


def test_health_cooldowns_distinguish_canonical_capacity_failures():
    assert controller.cooldown_seconds("success") == 0
    assert controller.cooldown_seconds("no_work") == 0
    assert controller.cooldown_seconds("work_failure") == 0
    assert controller.cooldown_seconds("provider_failure") == 15 * 60
    assert controller.cooldown_seconds("provider_throttle") == 30 * 60
    assert controller.cooldown_seconds("provider_quota") == 30 * 60
    assert controller.cooldown_seconds("model_unavailable") == 6 * 60 * 60
    assert controller.cooldown_seconds("model_retired_410") == 6 * 60 * 60
    assert controller.cooldown_seconds("model_policy_violation") == 6 * 60 * 60
    assert controller.cooldown_seconds("environment_failure") == 15 * 60
    assert controller.cooldown_seconds("control_plane_failure") == 15 * 60
    assert controller.cooldown_seconds("unknown_failure") == 15 * 60


def test_health_cooldowns_keep_legacy_records_readable():
    assert controller.cooldown_seconds("failure") == 15 * 60
    assert controller.cooldown_seconds("RATE LIMITED") == 30 * 60
    assert controller.cooldown_seconds("MODEL MISSING") == 6 * 60 * 60
    assert controller.cooldown_seconds("provider_unavailable") == 30 * 60
    assert controller.cooldown_seconds("model_interruption") == 15 * 60
    assert controller.cooldown_seconds("worker_environment_failure") == 15 * 60
    assert controller.cooldown_seconds("no_change") == 0
    assert controller.cooldown_seconds("policy_blocked") == 0


def test_latest_worker_health_uses_newest_heartbeat():
    comments = [
        {
            "body": "Worker: opencode-free-model-factory-41\nOutcome: RATE LIMITED\nUpdated: 2026-08-24T11:00:00Z"
        },
        {
            "body": "Worker: opencode-free-model-factory-41\nOutcome: success\nUpdated: 2026-08-24T11:20:00Z"
        },
    ]
    health = controller.latest_worker_health(comments)
    assert health["41"][0] == "success"


def test_classified_attempt_outcome_survives_newer_liveness_heartbeat():
    comments = [
        {
            "body": (
                "Worker: opencode-free-model-factory-41\n"
                "Outcome: MODEL MISSING\n"
                "Attempt outcome: model_unavailable\n"
                "Updated: 2026-08-24T11:00:00Z"
            )
        },
        {
            "body": (
                "Worker: opencode-free-model-factory-41\n"
                "Outcome: running\n"
                "Updated: 2026-08-24T11:20:00Z"
            )
        },
    ]
    health = controller.latest_worker_health(comments)
    assert health["41"][0] == "model_unavailable"


def test_newest_classified_attempt_replaces_older_attempt():
    comments = [
        {
            "body": (
                "Worker: opencode-free-model-factory-41\n"
                "Attempt outcome: provider_failure\n"
                "Updated: 2026-08-24T11:00:00Z"
            )
        },
        {
            "body": (
                "Worker: opencode-free-model-factory-41\n"
                "Attempt outcome: success\n"
                "Updated: 2026-08-24T11:20:00Z"
            )
        },
    ]
    health = controller.latest_worker_health(comments)
    assert health["41"][0] == "success"


def test_completion_selection_skips_owned_and_cooling_workers():
    now = controller.parse_time("2026-08-24T12:00:00Z")
    assert now is not None
    workers = [str(worker) for worker in range(6, 30)]
    health = {
        **dict.fromkeys(workers, ("success", now - 60)),
        "6": ("provider_failure", now - 60),
        "10": ("provider_throttle", now - 60),
        "14": ("model_unavailable", now - 60),
    }
    selected = controller.select_completion_workers(
        workers,
        review_backlog=80,
        owned_workers={"7", "8"},
        health=health,
        now_epoch=now,
    )
    assert len(selected) == 12
    assert not {"6", "7", "8", "10", "14"} & set(selected)


def test_completion_selection_prefers_review_capacity_at_high_backlog():
    workers = ["9", "10", "19", "20", "29", "30", "39", "40", "49", "50", "59", "60", "69", "70"]
    health = dict.fromkeys(workers, ("success", 0))
    selected = controller.select_completion_workers(
        workers,
        review_backlog=80,
        health=health,
        now_epoch=1,
    )
    review_first = [
        worker
        for worker in selected
        if controller.review_capacity_worker(worker, review_backlog=80)
    ]
    assert selected[: len(review_first)] == review_first
    assert len(selected) == 12


def test_unproven_dynamic_slot_is_unknown_but_may_run_first_execution():
    """Dynamic catalog slots need one real execution to establish health (#5213)."""
    assert controller.worker_health_state("41", {}, now_epoch=100) == "unknown"
    assert controller.worker_is_executable("41", {}, now_epoch=100)


def test_health_state_distinguishes_unavailable_cooling_and_recovery():
    now = controller.parse_time("2026-08-24T17:00:00Z")
    assert now is not None
    assert (
        controller.worker_health_state(
            "41",
            {"41": ("model_unavailable", now - 86400)},
            now_epoch=now,
        )
        == "unavailable"
    )
    assert (
        controller.worker_health_state(
            "41",
            {"41": ("model_policy_violation", now - 60)},
            now_epoch=now,
        )
        == "unavailable"
    )
    assert (
        controller.worker_health_state(
            "41",
            {"41": ("provider_throttle", now - 60)},
            now_epoch=now,
        )
        == "cooling"
    )
    assert (
        controller.worker_health_state(
            "41",
            {"41": ("provider_failure", now - 3600)},
            now_epoch=now,
        )
        == "degraded"
    )
    assert (
        controller.worker_health_state(
            "41",
            {"41": ("success", now)},
            now_epoch=now,
        )
        == "healthy"
    )


def test_no_work_keeps_worker_executable():
    assert controller.worker_health_state(
        "41", {"41": ("no_work", 100)}, now_epoch=100
    ) == "healthy"


def test_capacity_report_names_only_executable_candidates():
    """Unavailable models are excluded; unproven slots may run once (#5213)."""
    candidates = [
        {"worker": "41", "provider": "opencode-free", "model": "mimo"},
        {"worker": "42", "provider": "nvidia", "model": "retired"},
        {"worker": "43", "provider": "openrouter-free", "model": "unknown"},
    ]
    health = {
        "41": ("success", 100),
        "42": ("model_unavailable", 100),
    }
    report = controller.capacity_report(candidates, health, now_epoch=100)
    assert report["executable_capacity"] == 2
    assert report["health_counts"] == {
        "unknown": 1,
        "healthy": 1,
        "degraded": 0,
        "cooling": 0,
        "unavailable": 1,
    }
    assert report["executable_candidates"] == [
        {"worker": "41", "provider": "opencode-free", "model": "mimo", "health": "healthy"},
        {"worker": "43", "provider": "openrouter-free", "model": "unknown", "health": "unknown"},
    ]
    assert report["executable_slot_capacity"] == 2
    assert report["candidate_health_counts"] == report["health_counts"]
    assert report["executable_candidate_count"] == 2
    assert report["executable_provider_models"] == [
        {"provider": "opencode-free", "model": "mimo", "health": "healthy"},
        {"provider": "openrouter-free", "model": "unknown", "health": "unknown"},
    ]
    assert all(row["worker"] != "42" for row in report["executable_candidates"])


def test_capacity_report_does_not_count_repeated_slots_as_distinct_candidates():
    candidates = [
        {"worker": "60", "provider": "openrouter-free", "model": "cohere/north-mini-code:free"},
        {"worker": "61", "provider": "openrouter-free", "model": "cohere/north-mini-code:free"},
        {"worker": "62", "provider": "openrouter-free", "model": "cohere/north-mini-code:free"},
    ]
    health = dict.fromkeys(("60", "61", "62"), ("success", 100))

    report = controller.capacity_report(candidates, health, now_epoch=100)

    assert report["executable_slot_capacity"] == 3
    assert report["slot_health_counts"]["healthy"] == 3
    assert report["executable_candidate_count"] == 1
    assert report["candidate_health_counts"]["healthy"] == 1
    assert report["executable_provider_models"] == [
        {
            "provider": "openrouter-free",
            "model": "cohere/north-mini-code:free",
            "health": "healthy",
        }
    ]


def test_batch_executability_reads_each_candidate_once():
    """Repeated worker ranking reuses one live read per candidate."""
    calls: list[int] = []

    class FakeController:
        @staticmethod
        def candidate_is_live_executable(candidate):
            calls.append(candidate.number)
            return candidate.number != 42

    class FakeCandidate:
        def __init__(self, number: int) -> None:
            self.number = number

    cache: dict[int, bool] = {}
    allowed = FakeCandidate(41)
    blocked = FakeCandidate(42)

    assert controller.candidate_is_batch_executable(FakeController, allowed, cache)
    assert controller.candidate_is_batch_executable(FakeController, allowed, cache)
    assert not controller.candidate_is_batch_executable(FakeController, blocked, cache)
    assert not controller.candidate_is_batch_executable(FakeController, blocked, cache)
    assert calls == [41, 42]


def test_catalog_success_makes_peer_capability_slots_executable():
    """A real model success proves capacity for idle slots sharing its catalog."""
    comments = [
        {
            "author_association": "OWNER",
            "body": (
                "Worker: opencode-free-model-factory-39\n"
                "Source: opencode-free\n"
                "Model: model-a\n"
                "Attempt outcome: success\n"
                "Updated: 2026-08-24T12:00:00Z"
            ),
        }
    ]
    candidates = [
        {"worker": "39", "provider": "opencode-free", "model": "model-a"},
        {"worker": "40", "provider": "opencode-free", "model": "model-b"},
    ]
    now = controller.parse_time("2026-08-24T12:01:00Z")
    assert now is not None

    health = controller.latest_worker_health(
        comments,
        candidates=candidates,
        now_epoch=now,
    )

    assert controller.worker_is_executable("39", health, now_epoch=now)
    assert controller.worker_is_executable("40", health, now_epoch=now)


def test_catalog_model_failure_cools_only_its_own_slot():
    """A failed catalog model must not suppress peer models on its provider.

    Provider/account-wide breakers live in Neon account backoffs; one model's
    provider_failure is model-scoped evidence only.
    """
    comments = [
        {
            "author_association": "OWNER",
            "body": (
                "Worker: opencode-free-model-factory-39\n"
                "Source: opencode-free\n"
                "Model: model-a\n"
                "Attempt outcome: provider_failure\n"
                "Updated: 2026-08-24T12:00:00Z"
            ),
        }
    ]
    candidates = [
        {"worker": "39", "provider": "opencode-free", "model": "model-a"},
        {"worker": "40", "provider": "opencode-free", "model": "model-b"},
    ]
    now = controller.parse_time("2026-08-24T12:01:00Z")
    assert now is not None

    health = controller.latest_worker_health(
        comments,
        candidates=candidates,
        now_epoch=now,
    )

    assert controller.worker_health_state("39", health, now_epoch=now) == "cooling"
    assert controller.worker_health_state("40", health, now_epoch=now) != "cooling"
    assert controller.worker_is_executable("40", health, now_epoch=now)


def test_legacy_catalog_provider_outage_cools_only_its_own_slot():
    """Historical provider_unavailable evidence stays actionable for its slot."""
    comments = [
        {
            "author_association": "OWNER",
            "body": (
                "Worker: opencode-free-model-factory-39\n"
                "Source: opencode-free\n"
                "Model: model-a\n"
                "Attempt outcome: provider_unavailable\n"
                "Updated: 2026-08-24T12:00:00Z"
            ),
        }
    ]
    candidates = [
        {"worker": "39", "provider": "opencode-free", "model": "model-a"},
        {"worker": "40", "provider": "opencode-free", "model": "model-b"},
    ]
    now = controller.parse_time("2026-08-24T12:01:00Z")
    assert now is not None

    health = controller.latest_worker_health(
        comments,
        candidates=candidates,
        now_epoch=now,
    )

    assert controller.worker_health_state("39", health, now_epoch=now) == "cooling"
    assert controller.worker_health_state("40", health, now_epoch=now) != "cooling"
    assert controller.worker_is_executable("40", health, now_epoch=now)


def test_native_omniroute_intents_are_executable_without_backing_model_health():
    """Native OmniRoute routes do not wait on concrete-model attempt history."""
    candidates = [
        {"worker": "41", "provider": "omniroute-free", "model": "auto/coding:free"},
        {"worker": "42", "provider": "omniroute-free", "model": "auto/reasoning:free"},
    ]
    now = controller.parse_time("2026-08-24T12:01:00Z")
    assert now is not None

    health = controller.latest_worker_health(
        [],
        candidates=candidates,
        now_epoch=now,
    )

    assert controller.worker_is_executable("41", health, now_epoch=now)
    assert controller.worker_is_executable("42", health, now_epoch=now)


def test_completion_controller_uses_dispatcher_not_direct_assign() -> None:
    """Completion drain must signal the dispatcher (assign) instead of calling assign_candidate directly."""
    text = SCRIPT.read_text(encoding="utf-8")
    # The completion drain (signal_completion_mode) must not directly assign
    # candidates; it returns an empty assignments list.
    drain_body = text.split("def signal_completion_mode")[1].split("def assign_completion_batch")[0]
    assert "assign_candidate" not in drain_body, (
        "signal_completion_mode must not call assign_candidate directly; "
        "it must use the dispatcher (assign) instead"
    )
    assert "assignments" in drain_body, (
        "signal_completion_mode must return an empty assignments list"
    )
    # The dispatcher lane (assign) remains the only direct assignment path.
    controller_text = CONTROLLER_PATH.read_text(encoding="utf-8")
    assert "def assign(worker" in controller_text, (
        "the dispatcher assign() function must remain the single assignment path"
    )


def test_factory_work_controller_assign_has_kinds_param() -> None:
    """factory-work-controller.py assign() must accept a kinds parameter."""
    text = CONTROLLER_PATH.read_text(encoding="utf-8")
    assert "def assign(worker: str, kinds:" in text, (
        "assign() must accept a kinds parameter for filtering candidates"
    )
    assert "if kinds is not None" in text, (
        "assign() must filter candidates when kinds is provided"
    )
