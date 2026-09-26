"""Regression coverage for native OmniRoute intent selection."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
SCRIPT = SCRIPTS / "factory_candidate_health.py"


def load_module() -> ModuleType:
    """Load the selector with its sibling script imports available."""
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("factory_candidate_health_native", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HEALTH = load_module()


def candidate(route: str) -> dict[str, str]:
    """Build one normalized native OmniRoute virtual route candidate."""
    return {
        "provider": "omniroute-free",
        "model": route,
        "runtime_model": f"omniroute/{route}",
        "discovered_by": "native_auto_route_fallback",
    }


def test_native_coding_route_does_not_require_prior_model_health() -> None:
    selection = HEALTH.select_candidate(
        [candidate("auto/coding:free")],
        [],
        worker=41,
        now_epoch=1_788_619_500,
        preferred_provider="omniroute-free",
    )

    assert selection.selected is not None
    assert selection.selected.model == "auto/coding:free"
    assert selection.selected.runtime_model == "omniroute/auto/coding:free"
    assert selection.selected.health_state == "native_route"
    assert selection.failure_outcome == ""
    assert "smoke owns executable health" in selection.detail


def test_native_reasoning_route_does_not_require_prior_model_health() -> None:
    selection = HEALTH.select_candidate(
        [candidate("auto/reasoning:free")],
        [],
        worker=7,
        now_epoch=1_788_619_500,
        preferred_provider="omniroute-free",
    )

    assert selection.selected is not None
    assert selection.selected.model == "auto/reasoning:free"
    assert selection.selected.runtime_model == "omniroute/auto/reasoning:free"
    assert selection.selected.health_state == "native_route"


def test_malformed_native_runtime_selector_still_fails_closed() -> None:
    malformed = candidate("auto/coding:free")
    malformed["runtime_model"] = "openrouter/some-model"

    selection = HEALTH.select_candidate(
        [malformed],
        [],
        worker=1,
        now_epoch=1_788_619_500,
        preferred_provider="omniroute-free",
    )

    assert selection.selected is None
    assert selection.failure_outcome == "unknown_failure"


def test_review_intent_is_not_replaced_by_a_concrete_backing_model() -> None:
    """A resolved reasoning intent stays virtual even when concrete models exist."""
    selection = HEALTH.select_candidate(
        [
            candidate("auto/reasoning:free"),
            {
                "provider": "omniroute-free",
                "model": "vendor/concrete:free",
                "runtime_model": "omniroute/vendor/concrete:free",
                "discovered_by": "provider_catalog",
            },
        ],
        [],
        worker=99,
        now_epoch=1_788_619_500,
        preferred_provider="omniroute-free",
    )

    assert selection.selected is not None
    assert selection.selected.model == "auto/reasoning:free"
    assert selection.selected.runtime_model == "omniroute/auto/reasoning:free"
    assert selection.selected.health_state == "native_route"


def test_coding_lane_does_not_consult_backing_model_health() -> None:
    """A coding intent reaches selection with no catalog health evidence."""
    selection = HEALTH.select_candidate(
        [
            candidate("auto/coding:free"),
            {
                "provider": "omniroute-free",
                "model": "vendor/concrete:free",
                "runtime_model": "omniroute/vendor/concrete:free",
                "discovered_by": "provider_catalog",
            },
        ],
        [],
        worker=41,
        now_epoch=1_788_619_500,
        preferred_provider="omniroute-free",
        require_rankings=True,
        rankings={"vendor/concrete": 0.99},
    )

    assert selection.selected is not None
    assert selection.selected.model == "auto/coding:free"
    assert "smoke owns executable health" in selection.detail


def test_resolved_native_intent_is_not_rotated_by_worker_identity() -> None:
    """Dispatch carries the already-resolved intent instead of picking by slot."""
    selection = HEALTH.select_candidate(
        [candidate("auto/reasoning:free"), candidate("auto/coding:free")],
        [],
        worker=2,
        now_epoch=1_788_619_500,
        preferred_provider="omniroute-free",
    )

    assert selection.selected is not None
    assert selection.selected.model == "auto/reasoning:free"


def test_rank_candidates_does_not_health_filter_native_intents() -> None:
    """Native OmniRoute intents stay executable without backing-model history."""
    ranked = HEALTH.rank_candidates(
        [candidate("auto/coding:free"), candidate("auto/reasoning:free")],
        [],
        now_epoch=1_788_619_500,
    )

    assert {item.model: item.health_state for item in ranked} == {
        "auto/coding:free": "native_route",
        "auto/reasoning:free": "native_route",
    }


def test_unknown_concrete_omniroute_model_still_needs_health_evidence() -> None:
    selection = HEALTH.select_candidate(
        [
            {
                "provider": "omniroute-free",
                "model": "vendor/concrete:free",
                "runtime_model": "omniroute/vendor/concrete:free",
                "discovered_by": "provider_catalog",
            }
        ],
        [],
        worker=1,
        now_epoch=1_788_619_500,
        preferred_provider="omniroute-free",
    )

    assert selection.selected is None
    assert selection.failure_outcome == "unknown_failure"
