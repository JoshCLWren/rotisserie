"""Tests for centralized factory provider candidate adapters."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "factory_provider_candidates.py"
)


def load_module() -> ModuleType:
    """Load the script module without requiring .github to be a package."""
    spec = importlib.util.spec_from_file_location("factory_provider_candidates", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CANDIDATES = load_module()


def test_nvidia_requires_runtime_evidence_without_account_catalog() -> None:
    """NVIDIA candidates are not guessed without reliable enumeration."""
    result = CANDIDATES.discover(
        "nvidia",
        json.dumps({"data": [{"id": "vendor/new-code-model"}]}),
        ["vendor/new-code-model"],
    )

    assert result.mode == "runtime_only"
    assert result.status == "indeterminate"
    assert result.candidates == ()


def test_openrouter_requires_catalog_evidence_of_free_cost() -> None:
    """Paid OpenRouter models never enter the free candidate pool."""
    result = CANDIDATES.discover(
        "openrouter-free",
        json.dumps(
            {
                "data": [
                    {
                        "id": "vendor/free-by-price",
                        "pricing": {"prompt": "0", "completion": "0.000"},
                    },
                    {
                        "id": "vendor/free-suffix:free",
                        "pricing": {"prompt": "1", "completion": "1"},
                    },
                    {
                        "id": "vendor/paid",
                        "pricing": {"prompt": "0.1", "completion": "0.2"},
                    },
                ]
            }
        ),
    )

    assert [candidate.model for candidate in result.candidates] == [
        "vendor/free-by-price",
        "vendor/free-suffix:free",
    ]
    assert all(
        candidate.runtime_model.startswith("openrouter/")
        for candidate in result.candidates
    )


def test_opencode_uses_project_available_cli_catalog() -> None:
    """Only OpenCode selectors exposed to the current project are candidates."""
    result = CANDIDATES.discover(
        "opencode-free",
        "opencode/new-free\nopenrouter/not-this-provider\ninvalid\n",
        ["new-free", "missing-free"],
    )

    assert result.status == "available"
    assert [candidate.model for candidate in result.candidates] == ["new-free"]
    assert result.candidates[0].discovered_by == "opencode_models"



def test_omniroute_exposes_native_free_intent_routes() -> None:
    """Factories choose work intent while OmniRoute chooses the backing model."""
    result = CANDIDATES.discover(
        "omniroute-free",
        json.dumps(
            {
                "data": [
                    {"id": "free-cascade-small"},
                    {"id": "auto/coding:free"},
                    {"id": "auto/reasoning:free"},
                    {"id": "provider/backing-model:free"},
                ]
            }
        ),
    )

    assert result.status == "available"
    assert [candidate.model for candidate in result.candidates] == [
        "auto/coding:free",
        "auto/reasoning:free",
    ]
    assert [candidate.runtime_model for candidate in result.candidates] == [
        "omniroute/auto/coding:free",
        "omniroute/auto/reasoning:free",
    ]
    assert {candidate.discovered_by for candidate in result.candidates} == {
        "provider_catalog"
    }


def test_omniroute_native_route_survives_catalog_omission() -> None:
    """Virtual native routes remain addressable when /models omits them transiently."""
    result = CANDIDATES.discover(
        "omniroute-free",
        json.dumps({"data": [{"id": "provider/backing-model:free"}]}),
    )

    assert [candidate.model for candidate in result.candidates] == [
        "auto/coding:free",
        "auto/reasoning:free",
    ]
    assert {candidate.discovered_by for candidate in result.candidates} == {
        "native_auto_route_fallback"
    }


def test_omniroute_does_not_enumerate_backing_models() -> None:
    """Catalog backing models never become ComicPile-selected factory capacity."""
    result = CANDIDATES.discover(
        "omniroute-free",
        json.dumps(
            {
                "data": [
                    {"id": "provider/backing-model:free"},
                    {"id": "another/paid-model"},
                    {"id": "free-cascade-big"},
                ]
            }
        ),
    )

    assert all(
        candidate.model in {"auto/coding:free", "auto/reasoning:free"}
        for candidate in result.candidates
    )
    assert "provider/backing-model:free" not in {
        candidate.model for candidate in result.candidates
    }


def test_omniroute_configured_filter_can_exclude_native_route() -> None:
    result = CANDIDATES.discover(
        "omniroute-free",
        json.dumps({"data": [{"id": "auto/coding:free"}]}),
        ["some-other-route"],
    )

    assert result.status == "empty"
    assert result.candidates == ()


def test_invalid_catalog_fails_closed() -> None:
    """Malformed catalog responses produce no executable candidates."""
    result = CANDIDATES.discover("openrouter-free", "service unavailable")

    assert result.status == "invalid"
    assert result.candidates == ()


def test_empty_catalog_is_not_capacity() -> None:
    """A valid catalog with no eligible entries contributes no capacity."""
    result = CANDIDATES.discover(
        "openrouter-free",
        json.dumps(
            {
                "data": [
                    {
                        "id": "vendor/paid",
                        "pricing": {"prompt": "1", "completion": "1"},
                    }
                ]
            }
        ),
    )

    assert result.status == "empty"
    assert result.candidates == ()


def test_kilo_auto_encapsulates_non_enumerating_route() -> None:
    """Kilo Auto waits for real runtime evidence instead of guessing models."""
    result = CANDIDATES.discover("kilo-auto", "ignored")

    assert result.mode == "runtime_only"
    assert result.status == "indeterminate"
    assert result.candidates == ()


def test_z_ai_and_ollama_cloud_require_runtime_evidence() -> None:
    """Fixed OpenAI-compatible free pins are not guessed from catalogs."""
    z_ai = CANDIDATES.discover("z-ai", json.dumps({"data": [{"id": "glm-4.5-flash"}]}))
    ollama = CANDIDATES.discover(
        "ollama-cloud",
        json.dumps({"data": [{"id": "nemotron-3-nano:30b"}]}),
    )

    assert z_ai.mode == "runtime_only"
    assert z_ai.status == "indeterminate"
    assert z_ai.candidates == ()
    assert ollama.mode == "runtime_only"
    assert ollama.status == "indeterminate"
    assert ollama.candidates == ()


def test_unknown_provider_is_rejected() -> None:
    """Unregistered providers cannot silently become executable capacity."""
    try:
        CANDIDATES.discover("mystery-provider", "{}")
    except ValueError as exc:
        assert "unsupported factory provider" in str(exc)
    else:
        raise AssertionError("unknown provider did not fail closed")
