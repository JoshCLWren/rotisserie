"""Tests for health-aware provider candidate selection."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "factory_candidate_health.py"
)
sys.path.insert(0, str(SCRIPT.parent))


def load_module() -> ModuleType:
    """Load the health selector without packaging .github."""
    spec = importlib.util.spec_from_file_location("factory_candidate_health", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HEALTH = load_module()
NOW = 2_000_000
CANDIDATES = [
    {
        "provider": "openrouter-free",
        "model": "vendor/a:free",
        "runtime_model": "openrouter/vendor/a:free",
        "discovered_by": "provider_catalog",
    },
    {
        "provider": "openrouter-free",
        "model": "vendor/b:free",
        "runtime_model": "openrouter/vendor/b:free",
        "discovered_by": "provider_catalog",
    },
]


def evidence(
    model: str,
    outcome: str,
    age: int = 0,
    *,
    provider: str = "openrouter-free",
    trusted: bool = True,
) -> dict[str, object]:
    """Build evidence relative to the fixed test clock."""
    timestamp = datetime.fromtimestamp(NOW - age, tz=UTC)
    return {
        "author_association": "OWNER" if trusted else "NONE",
        "body": (
            "<!-- factory-attempt-outcome:v1 -->\n"
            f"Model: {model}\n"
            f"Source: {provider}\n"
            f"Attempt outcome: {outcome}\n"
            f"Updated: {timestamp.isoformat().replace('+00:00', 'Z')}\n"
        ),
    }


def test_successful_execution_is_preferred_to_unknown_candidate() -> None:
    """Actual successful execution outweighs catalog-only discovery."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [evidence("vendor/b:free", "success")],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"
    assert result.selected.health_state == "healthy"


def test_arena_code_ranking_prefers_strongest_qualified_route() -> None:
    """A live Arena code score orders healthy free routes deterministically."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [evidence("vendor/a:free", "success"), evidence("vendor/b:free", "success")],
        worker=1,
        now_epoch=NOW,
        rankings={"vendor/a": 0.41, "vendor/b": 0.73},
    )

    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"


def test_unranked_route_is_fallback_when_no_ranked_route_is_usable() -> None:
    """A roster change does not make the factory unusable when rankings lag."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [
            evidence("vendor/a:free", "model_unavailable"),
            evidence("vendor/b:free", "success"),
        ],
        worker=1,
        now_epoch=NOW,
        rankings={"vendor/a": 0.73},
    )

    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"


def test_required_ranking_keeps_successful_unranked_routes() -> None:
    """Arena membership must not erase eligible free fallback capacity."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [evidence("vendor/a:free", "success"), evidence("vendor/b:free", "success")],
        worker=1,
        now_epoch=NOW,
        require_rankings=True,
    )

    assert result.selected is not None
    assert result.selected.model in {"vendor/a:free", "vendor/b:free"}
    assert result.failure_outcome == ""
    assert {candidate.model for candidate in result.candidates} == {
        "vendor/a:free",
        "vendor/b:free",
    }


def test_zero_arena_overlap_does_not_create_no_capacity_sentinel() -> None:
    """Eligible free models remain routable when Arena has no identity matches."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [evidence("vendor/a:free", "success"), evidence("vendor/b:free", "success")],
        worker=1,
        now_epoch=NOW,
        rankings={"some-other-arena-model": 0.99},
        require_rankings=True,
    )

    assert result.selected is not None
    assert result.selected.model in {"vendor/a:free", "vendor/b:free"}
    assert result.failure_outcome == ""


def test_arena_orders_matched_models_without_dropping_unranked_fallbacks() -> None:
    """Arena may prefer a scored route while unranked free models stay eligible."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [evidence("vendor/a:free", "success"), evidence("vendor/b:free", "success")],
        worker=1,
        now_epoch=NOW,
        rankings={"vendor/b": 0.73},
        require_rankings=True,
    )

    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"
    assert {candidate.model for candidate in result.candidates} == {
        "vendor/a:free",
        "vendor/b:free",
    }


def test_expired_ranking_file_is_not_usable(tmp_path) -> None:
    """A stale last-known-good feed cannot admit new factory capacity."""
    path = tmp_path / "rankings.json"
    path.write_text(json.dumps({"fetched_at": 1, "models": []}), encoding="utf-8")

    assert HEALTH.load_code_rankings(path) == {}


def test_preferred_provider_lane_uses_usable_provider_before_global_rank() -> None:
    """The OmniRoute canary lane stays on OmniRoute when its route is usable."""
    candidates = [
        *CANDIDATES,
        {
            "provider": "omniroute-free",
            "model": "auto/coding:free",
            "runtime_model": "omniroute/auto/coding:free",
            "discovered_by": "provider_catalog",
        },
    ]
    result = HEALTH.select_candidate(
        candidates,
        [
            evidence("vendor/a:free", "success"),
            evidence("auto/coding:free", "success", provider="omniroute-free"),
        ],
        worker=45,
        now_epoch=NOW,
        preferred_provider="omniroute-free",
    )

    assert result.selected is not None
    assert result.selected.provider == "omniroute-free"


def test_omniroute_throttle_is_scoped_to_one_model_route() -> None:
    """A throttled OmniRoute model must not suppress alternate gateway routes."""
    candidates = [
        {
            "provider": "omniroute-free",
            "model": "vendor/a:free",
            "runtime_model": "omniroute/vendor/a:free",
            "discovered_by": "provider_catalog",
        },
        {
            "provider": "omniroute-free",
            "model": "vendor/b:free",
            "runtime_model": "omniroute/vendor/b:free",
            "discovered_by": "provider_catalog",
        },
    ]
    result = HEALTH.select_candidate(
        candidates,
        [
            evidence("vendor/a:free", "provider_throttle", provider="omniroute-free"),
            evidence("vendor/b:free", "success", provider="omniroute-free"),
        ],
        worker=45,
        now_epoch=NOW,
        preferred_provider="omniroute-free",
    )

    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"


def test_omniroute_410_still_retires_only_that_model() -> None:
    """Permanent retirement remains model-specific behind the shared gateway."""
    candidates = [
        {
            "provider": "omniroute-free",
            "model": "vendor/a:free",
            "runtime_model": "omniroute/vendor/a:free",
            "discovered_by": "provider_catalog",
        },
        {
            "provider": "omniroute-free",
            "model": "vendor/b:free",
            "runtime_model": "omniroute/vendor/b:free",
            "discovered_by": "provider_catalog",
        },
    ]
    result = HEALTH.select_candidate(
        candidates,
        [
            evidence("vendor/a:free", "model_retired_410", provider="omniroute-free"),
            evidence("vendor/b:free", "success", provider="omniroute-free"),
        ],
        worker=45,
        now_epoch=NOW,
        preferred_provider="omniroute-free",
    )

    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"


def test_no_work_keeps_candidate_healthy() -> None:
    """Canonical no_work proves executor health without requiring a diff."""
    result = HEALTH.select_candidate(
        [CANDIDATES[0]],
        [evidence("vendor/a:free", "no_work")],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is not None
    assert result.selected.health_state == "healthy"


def test_permanently_unavailable_model_is_excluded() -> None:
    """Explicit model retirement never remains executable capacity."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [
            evidence("vendor/a:free", "model_unavailable"),
            evidence("vendor/b:free", "success"),
        ],
        worker=1,
        now_epoch=NOW,
    )

    states = {candidate.model: candidate.health_state for candidate in result.candidates}
    assert states["vendor/a:free"] == "unavailable"
    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"


def test_http_410_retirement_marker_remains_excluded_after_newer_success() -> None:
    """A provider's 410 retirement marker permanently blacklists that model."""
    retired = evidence("vendor/a:free", "success", age=60)
    retired["body"] = (
        "<!-- factory-model-retired-410:v1 -->\n"
        "Model: vendor/a:free\n"
        "Source: openrouter-free\n"
        "Reason: provider returned HTTP 410 Gone\n"
        "Updated: 1970-01-23T03:33:20Z\n"
    )
    result = HEALTH.select_candidate(
        CANDIDATES,
        [retired, evidence("vendor/a:free", "success"), evidence("vendor/b:free", "success")],
        worker=1,
        now_epoch=NOW,
    )

    states = {candidate.model: candidate.health_state for candidate in result.candidates}
    assert states["vendor/a:free"] == "unavailable"
    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"


def test_model_policy_violation_is_model_scoped() -> None:
    """A permanent model policy failure does not blacklist sibling provider models."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [
            evidence("vendor/a:free", "model_policy_violation"),
            evidence("vendor/b:free", "success"),
        ],
        worker=1,
        now_epoch=NOW,
    )

    states = {candidate.model: candidate.health_state for candidate in result.candidates}
    assert states["vendor/a:free"] == "unavailable"
    assert states["vendor/b:free"] == "healthy"
    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"


def test_provider_failure_cools_every_provider_candidate() -> None:
    """Recent canonical provider-wide failure prevents dispatch to sibling models."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [evidence("vendor/a:free", "provider_failure")],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is None
    assert result.failure_outcome == "provider_failure"
    assert {candidate.health_state for candidate in result.candidates} == {"cooling"}


def test_provider_throttle_has_canonical_terminal_outcome() -> None:
    """Rate-limit evidence cools the provider and preserves throttle attribution."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [evidence("vendor/a:free", "provider_throttle")],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is None
    assert result.failure_outcome == "provider_throttle"


def test_legacy_provider_outage_is_read_but_reemitted_canonically() -> None:
    """Historical provider_unavailable evidence remains useful during migration."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [evidence("vendor/a:free", "provider_unavailable")],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is None
    assert result.failure_outcome == "provider_failure"
    assert {candidate.health_state for candidate in result.candidates} == {"cooling"}


def test_later_success_recovers_provider() -> None:
    """A newer actual success clears older provider outage evidence."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [
            evidence("vendor/a:free", "provider_failure", age=60),
            evidence("vendor/b:free", "success"),
        ],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is not None
    assert result.selected.model == "vendor/b:free"


def test_unknown_route_is_rejected_instead_of_being_used_as_fallback() -> None:
    """Selection must not spend a factory slot on an untested route."""
    result = HEALTH.select_candidate(
        CANDIDATES,
        [evidence("vendor/a:free", "unknown_failure", age=HEALTH.FAILURE_COOLDOWN_SECONDS + 1)],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is not None
    assert result.selected.model == "vendor/a:free"
    assert result.selected.health_state == "degraded"


def test_legacy_model_interruption_cools_then_degrades() -> None:
    """Historical interruption records remain readable but emit canonical failure."""
    recent = HEALTH.select_candidate(
        [CANDIDATES[0]],
        [evidence("vendor/a:free", "model_interruption")],
        worker=1,
        now_epoch=NOW,
    )
    old = HEALTH.select_candidate(
        [CANDIDATES[0]],
        [
            evidence(
                "vendor/a:free",
                "model_interruption",
                age=HEALTH.FAILURE_COOLDOWN_SECONDS + 1,
            )
        ],
        worker=1,
        now_epoch=NOW,
    )

    assert recent.selected is None
    assert recent.failure_outcome == "provider_failure"
    assert old.selected is not None
    assert old.selected.health_state == "degraded"


def test_unknown_failure_fails_closed_then_degrades() -> None:
    """Unknown failures cool first and recover only after the shared cooldown."""
    recent = HEALTH.select_candidate(
        [CANDIDATES[0]],
        [evidence("vendor/a:free", "unknown_failure")],
        worker=1,
        now_epoch=NOW,
    )
    old = HEALTH.select_candidate(
        [CANDIDATES[0]],
        [
            evidence(
                "vendor/a:free",
                "unknown_failure",
                age=HEALTH.FAILURE_COOLDOWN_SECONDS + 1,
            )
        ],
        worker=1,
        now_epoch=NOW,
    )

    assert recent.selected is None
    assert recent.failure_outcome == "unknown_failure"
    assert old.selected is not None
    assert old.selected.health_state == "degraded"


def test_control_plane_failure_does_not_poison_model() -> None:
    """Controller faults do not replace prior executor health evidence."""
    result = HEALTH.select_candidate(
        [CANDIDATES[0]],
        [
            evidence("vendor/a:free", "success", age=60),
            evidence("vendor/a:free", "control_plane_failure"),
        ],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is not None
    assert result.selected.health_state == "healthy"


def test_environment_failure_does_not_poison_model() -> None:
    """Runner failures do not replace prior provider/model health evidence."""
    result = HEALTH.select_candidate(
        [CANDIDATES[0]],
        [
            evidence("vendor/a:free", "success", age=60),
            evidence("vendor/a:free", "environment_failure"),
        ],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is not None
    assert result.selected.health_state == "healthy"


def test_untrusted_attempt_evidence_does_not_create_executable_capacity() -> None:
    """User-authored marker text cannot create executable capacity."""
    result = HEALTH.select_candidate(
        [CANDIDATES[0]],
        [evidence("vendor/a:free", "model_unavailable", trusted=False)],
        worker=1,
        now_epoch=NOW,
    )

    assert result.selected is None
    assert result.failure_outcome == "unknown_failure"
    assert result.candidates[0].health_state == "unknown"
