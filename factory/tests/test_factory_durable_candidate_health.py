"""No registry pagination required for catalog candidate selection."""
from __future__ import annotations
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github/scripts"))
health = importlib.import_module("factory_candidate_health")


def test_durable_healthy_route_is_executable_without_trusted_comment() -> None:
    now = 1_800_000_000
    evidence = health.durable_attempt_evidence({
        "models": [{"source": "opencode-free", "model": "alpha", "status": "healthy",
                    "outcome": "success", "updated": now - 5, "until": None}],
        "providers": [],
    }, now_epoch=now)
    result = health.select_candidate(
        [{"provider": "opencode-free", "model": "alpha",
          "runtime_model": "alpha", "discovered_by": "live_catalog"}],
        [], worker=1, now_epoch=now, evidence_override=evidence,
    )
    assert result.selected is not None
    assert result.selected.health_state == "healthy"


def test_old_provider_quota_does_not_block_a_healthy_sibling() -> None:
    now = 1_800_000_000
    evidence = health.durable_attempt_evidence({
        "models": [
            {"source": "requesty", "model": "healthy", "status": "healthy",
             "outcome": "success", "updated": now - 100, "until": None},
            {"source": "requesty", "model": "failed", "status": "cooling",
             "outcome": "provider_quota", "updated": now - 1, "until": now + 900},
        ],
        "providers": [{"source": "requesty", "outcome": "provider_quota",
                       "updated": now - 1, "until": now + 900}],
    }, now_epoch=now)
    result = health.select_candidate(
        [{"provider": "requesty", "model": model,
          "runtime_model": model, "discovered_by": "live_catalog"}
         for model in ("failed", "healthy")],
        [], worker=1, now_epoch=now, evidence_override=evidence,
    )
    assert result.selected is not None
    assert result.selected.model == "healthy"
    assert {item.model: item.health_state for item in result.candidates} == {
        "failed": "cooling", "healthy": "healthy",
    }


def test_missing_and_retired_models_never_become_healthy() -> None:
    now = 1_800_000_000
    evidence = health.durable_attempt_evidence({
        "models": [{"source": "requesty", "model": "retired", "status": "retired",
                    "outcome": "model_retired_410", "updated": now - 5}],
        "providers": [],
    }, now_epoch=now)
    candidates = [
        {"provider": "requesty", "model": model, "runtime_model": model,
         "discovered_by": "live_catalog"}
        for model in ("retired", "unseen")
    ]
    states = {r.model: r.health_state for r in health.rank_candidates(
        candidates, [], now_epoch=now, evidence_override=evidence)}
    assert states == {"retired": "unavailable", "unseen": "unknown"}
