"""OpenCode free models are selected from live catalog, not permanent worker pins."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
selector = importlib.import_module("factory_live_opencode")


def catalog(*entries: tuple[str, int, int]) -> str:
    return json.dumps(
        {
            "models": [
                {"id": model, "cost": {"input": inbound, "output": outbound}}
                for model, inbound, outbound in entries
            ],
        }
    )


def test_catalog_models_ignore_pricing_and_name_conventions() -> None:
    raw = catalog(("example-a", 0, 0), ("example-premium", 2, 3))
    assert selector.catalog_models(raw) == ["example-a", "example-premium"]


def test_new_catalog_models_rotate_without_roster() -> None:
    raw = catalog(("example-a", 0, 0), ("example-b", 2, 3), ("example-c", 0, 0))
    selected = {selector.select_model(raw, 41, turn)["model"] for turn in range(3)}
    assert selected == {"example-a", "example-b", "example-c"}


def test_worker_identity_not_bound_to_model_list() -> None:
    raw = catalog(("example-a", 0, 0), ("example-b", 0, 0))
    first = selector.select_model(raw, 41, 0)
    second = selector.select_model(raw, 41, 0)
    assert first == second
    assert first["candidate_count"] == 2


def test_empty_catalog_fails_without_model_guess() -> None:
    with pytest.raises(ValueError, match="no candidates"):
        selector.select_model(catalog(), 41, 0)


def attempt(model: str, updated: str, outcome: str = "model_unavailable") -> dict[str, object]:
    """Build the real trusted registry attempt shape, including its run identity."""
    return {
        "author_association": "OWNER",
        "body": (
            "<!-- factory-model-unavailable:v1 run=123 -->\n"
            f"Source: opencode-free\nModel: {model}\nAttempt outcome: {outcome}\n"
            f"Updated: {updated}\nRun: 123"
        ),
    }


def test_registry_cooldown_expires_and_is_scoped_to_model(tmp_path: Path) -> None:
    """A durable failure survives worker overwrites without suppressing other models."""
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([[attempt("ll-alpha-free", "2026-10-17T07:00:05Z")]]))
    from datetime import UTC, datetime

    now = int(datetime(2026, 10, 17, 7, 0, 5, tzinfo=UTC).timestamp())
    raw = catalog(("ll-alpha-free", 0, 0), ("big-pickle", 0, 0))
    selected = {
        selector.select_model(raw, 41, turn, None, comments, now_epoch=now)["model"]
        for turn in range(4)
    }
    assert selected == {"big-pickle"}
    selected_after_expiry = {
        selector.select_model(
            raw,
            41,
            turn,
            None,
            comments,
            now_epoch=now + selector.COOLDOWN_SECONDS,
        )["model"]
        for turn in range(4)
    }
    assert selected_after_expiry == {"big-pickle", "ll-alpha-free"}


def test_forged_malformed_stale_and_future_evidence_has_no_effect() -> None:
    """Only authenticated, well-formed, current registry attempts affect rotation."""
    from datetime import UTC, datetime

    now = int(datetime(2026, 10, 17, 7, 0, 5, tzinfo=UTC).timestamp())
    forged = attempt("forged-free", "2026-10-17T07:00:05Z")
    forged["author_association"] = "NONE"
    via_app = attempt("app-free", "2026-10-17T07:00:05Z")
    via_app["performed_via_github_app"] = {"slug": "untrusted-worker"}
    malformed = attempt("bad-free", "not-a-date")
    no_marker = attempt("no-marker-free", "2026-10-17T07:00:05Z")
    no_marker["body"] = str(no_marker["body"]).split("\n", 1)[1]
    no_run = attempt("no-run-free", "2026-10-17T07:00:05Z")
    no_run["body"] = str(no_run["body"]).rsplit("\n", 1)[0]
    assert (
        selector.unavailable_models(
            [
                forged,
                via_app,
                malformed,
                no_marker,
                no_run,
                attempt("old-free", "2026-10-16T07:00:05Z"),
                attempt("future-free", "2026-10-18T07:00:05Z"),
                attempt("environment-free", "2026-10-17T07:00:05Z", "environment_failure"),
                None,
                "bad record",
            ],
            now_epoch=now,
        )
        == set()
    )
    assert selector.unavailable_models({"unexpected": "shape"}, now_epoch=now) == set()


def test_all_candidates_unavailable_and_later_success(tmp_path: Path) -> None:
    """Exhaustion fails closed; a newer real success restores a temporary failure."""
    from datetime import UTC, datetime

    now = int(datetime(2026, 10, 17, 7, 0, 5, tzinfo=UTC).timestamp())
    comments = tmp_path / "comments.json"
    failure = attempt("big-pickle", "2026-10-17T07:00:05Z")
    comments.write_text(json.dumps([failure]))
    raw = catalog(("big-pickle", 0, 0))
    with pytest.raises(ValueError, match="All 1 candidates"):
        selector.select_model(raw, 41, 0, None, comments, now_epoch=now)
    comments.write_text(
        json.dumps([failure, attempt("big-pickle", "2026-10-17T07:00:06Z", "success")])
    )
    assert (
        selector.select_model(raw, 41, 0, None, comments, now_epoch=now + 1)["model"]
        == "big-pickle"
    )


@pytest.mark.parametrize(
    "log,expected",
    [
        ("Error: Model unavailable: opencode/ll-alpha-free", True),
        ("\x1b[91mError: \x1b[0mModel unavailable: opencode/ll-alpha-free", True),
        ("Error: Model unavailable: opencode/another-free", False),
        ("timeout after 180 seconds", False),
        ("Error: service temporarily unavailable", False),
    ],
)
def test_exact_smoke_failure_classification(tmp_path: Path, log: str, expected: bool) -> None:
    """Timeouts and other models cannot produce this model's unavailability."""
    target = tmp_path / "outcome"
    assert selector.record_smoke_failure(log, "opencode/ll-alpha-free", target) is expected
    assert target.exists() is expected
    if expected:
        assert (
            target.read_text() == "model_unavailable\tModel unavailable: opencode/ll-alpha-free\n"
        )


@pytest.mark.parametrize(
    "log,outcome",
    [
        ("HTTP 429 rate limit", "provider_throttle"),
        ("HTTP 402 budget exhausted", "provider_quota"),
    ],
)
def test_quota_and_throttle_are_provider_outcomes_not_model_unavailability(
    tmp_path: Path, log: str, outcome: str,
) -> None:
    """Rate/budget refusals keep their own class (#4779), never model_unavailable."""
    target = tmp_path / "outcome"
    assert selector.record_smoke_failure(log, "opencode/ll-alpha-free", target) is True
    assert target.read_text().split("\t", 1)[0] == outcome


@pytest.mark.parametrize(
    "log,runtime,expected",
    [
        ("Error: Model crouton-alpha-free has been deprecated.", "opencode/crouton-alpha-free", True),
        ("\x1b[91m\x1b[1mError: \x1b[0mModel crouton-alpha-free has been deprecated.", "opencode/crouton-alpha-free", True),
        ("Error: Model opencode/crouton-alpha-free has been deprecated.", "opencode/crouton-alpha-free", True),
        ("Model vendor/alpha:free has been deprecated", "openrouter/vendor/alpha:free", True),
        ("Model some-other-free has been deprecated", "opencode/crouton-alpha-free", False),
        ("Model crouton-alpha-free has been deprecated", "opencode/other-free", False),
    ],
)
def test_deprecated_smoke_model_rotates_only_exact_selector(
    tmp_path: Path, log: str, runtime: str, expected: bool,
) -> None:
    """A stale catalog model must rotate without poisoning unrelated models."""
    outcome = tmp_path / "outcome"
    assert selector.record_smoke_failure(log, runtime, outcome) is expected
    if expected:
        assert outcome.read_text() == f"model_unavailable\tModel deprecated: {runtime}\n"
    else:
        assert not outcome.exists()


def test_overloaded_model_is_throttle_not_deprecation(tmp_path: Path) -> None:
    outcome = tmp_path / "outcome"
    log = "HTTP 429, model crouton-alpha-free overloaded"
    assert selector.record_smoke_failure(log, "opencode/crouton-alpha-free", outcome)
    assert outcome.read_text().startswith("provider_throttle\t")


def test_cli_consults_registry_evidence(tmp_path: Path) -> None:
    """A real dispatch CLI call excludes the unavailable candidate from rotation."""
    import subprocess
    from datetime import UTC, datetime

    raw_catalog = tmp_path / "catalog.json"
    raw_catalog.write_text(catalog(("big-pickle", 0, 0), ("ll-alpha-free", 0, 0)))
    comments = tmp_path / "comments.json"
    comments.write_text(
        json.dumps(
            [
                attempt("ll-alpha-free", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ]
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / ".github/scripts/factory_live_opencode.py"),
            "--catalog",
            str(raw_catalog),
            "--comments",
            str(comments),
            "--worker",
            "42",
            "--rotation",
            "0",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["model"] == "big-pickle"


@pytest.mark.parametrize(
    "outcome",
    [
        "unknown_failure",
        "provider_failure",
        "provider_quota",
        "provider_throttle",
        "control_plane_failure",
        "environment_failure",
        "unconfigured",
    ],
)
def test_non_recovery_outcomes_do_not_clear_active_cooldown(outcome: str) -> None:
    """A later fallback-lane or infra report cannot erase unavailable-model evidence."""
    from datetime import UTC, datetime

    now = int(datetime(2026, 10, 17, 7, 0, 6, tzinfo=UTC).timestamp())
    failure = attempt("ll-alpha-free", "2026-10-17T07:00:05Z")
    later = attempt("ll-alpha-free", "2026-10-17T07:00:06Z", outcome)
    for records in ([failure, later], [later, failure]):
        assert selector.unavailable_models(records, now_epoch=now) == {"ll-alpha-free"}
        assert (
            selector.unavailable_models(
                records,
                now_epoch=now + selector.COOLDOWN_SECONDS,
            )
            == set()
        )


def test_openrouter_selector_requires_published_free_endpoint() -> None:
    raw = json.dumps({"models": [
        {"id": "openrouter/example/unpriced"},
        {"id": "openrouter/example/paid", "cost": {"input": 2, "output": 3}},
        {"id": "openrouter/example/available:free", "cost": {"input": 0, "output": 0}},
        {"id": "openrouter/example/second:free"},
        {"id": "opencode/another-source"},
    ]})
    assert selector.catalog_models(raw, provider="openrouter-free") == [
        "example/available:free", "example/second:free",
    ]
    selected = {selector.select_model(raw, 52, turn, provider="openrouter-free")["model"] for turn in range(2)}
    assert selected == {"example/available:free", "example/second:free"}


def test_model_unavailability_cooldown_is_provider_scoped(tmp_path: Path) -> None:
    """An OpenRouter failure cannot suppress the same model name on OpenCode."""
    from datetime import UTC, datetime

    now = int(datetime(2026, 11, 9, 23, 0, tzinfo=UTC).timestamp())
    entry = attempt("vendor/a:free", "2026-11-09T23:00:00Z")
    entry["body"] = str(entry["body"]).replace(
        "Source: opencode-free", "Source: openrouter-free"
    )
    assert selector.unavailable_models(
        [entry], provider="openrouter-free", now_epoch=now
    ) == {"vendor/a:free"}
    assert selector.unavailable_models(
        [entry], provider="opencode-free", now_epoch=now
    ) == set()


def test_openrouter_suffix_is_endpoint_contract_not_price_inference() -> None:
    raw = json.dumps({"models": [
        {"id": "openrouter/example/unknown"},
        {"id": "openrouter/example/price-zero", "cost": {"input": 0, "output": 0}},
        {"id": "openrouter/example/paid-free-label", "cost": {"input": 1, "output": 0}},
        {"id": "openrouter/example/free-route:free", "cost": {"input": 0, "output": 0}},
    ]})
    assert selector.catalog_models(raw, provider="openrouter-free") == [
        "example/free-route:free",
    ]


def test_neon_exclusions_take_precedence_over_live_free_catalog(tmp_path: Path) -> None:
    raw = catalog(("big-pickle", 0, 0), ("ll-alpha-free", 0, 0))
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text(json.dumps({"opencode-free": ["big-pickle"]}))
    selected = selector.select_model(raw, 41, 0, None, exclusions_path=exclusions)
    assert selected["model"] == "ll-alpha-free"
    exclusions.write_text(json.dumps({"opencode-free": ["big-pickle", "ll-alpha-free"]}))
    with pytest.raises(ValueError, match="unavailable or in cooldown"):
        selector.select_model(raw, 41, 0, None, exclusions_path=exclusions)
