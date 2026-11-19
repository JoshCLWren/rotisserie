"""Runtime model selection depends on provider catalogs, not inventory pins."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
SELECTOR = importlib.import_module("factory_runtime_model_select")
REGISTRY = importlib.import_module("factory_provider_registry")


@pytest.mark.parametrize("source", sorted(REGISTRY.PROVIDERS))
def test_registered_provider_can_choose_new_published_model(source: str) -> None:
    catalog = {"data": [{"id": "sample/newly-published-model", "pricing": {
        "prompt": "3", "completion": "5",
    }}]}
    selected = SELECTOR.select(source, "12345678901", catalog)
    assert selected["model"] == "sample/newly-published-model"
    assert selected["runtime_model"] == f"{REGISTRY.PROVIDERS[source].provider}/sample/newly-published-model"
    assert selected["catalog_listed"] is True


def test_price_does_not_exclude_candidates() -> None:
    catalog = {"data": [
        {"id": "sample/paid", "pricing": {"prompt": 1, "completion": 2}},
        {"id": "sample/unpriced"},
    ]}
    selected = SELECTOR.select("groq-free", "1234", catalog, {"groq-free": ["sample/unpriced"]})
    assert selected["model"] == "sample/paid"


def test_all_cooling_candidates_are_deferred() -> None:
    with pytest.raises(SELECTOR.CandidateUnavailableError):
        SELECTOR.select(
            "nvidia", "1234", {"data": [{"id": "sample/model"}]},
            {"nvidia": ["sample/model"]},
        )


def test_malformed_catalog_does_not_invent_a_model() -> None:
    with pytest.raises(SELECTOR.CandidateUnavailableError):
        SELECTOR.select("groq-free", "1234", {"data": {}})


def test_cloudflare_discovery_uses_official_model_search_not_inference_endpoint() -> None:
    spec = REGISTRY.PROVIDERS["cloudflare-workers-ai"]
    url = SELECTOR.catalog_url(
        "cloudflare-workers-ai", spec,
        {"CLOUDFLARE_ACCOUNT_ID": "synthetic-account"},
    )
    assert url == (
        "https://api.cloudflare.com/client/v4/accounts/synthetic-account"
        "/ai/models/search?format=openrouter&per_page=100"
    )


def test_gemini_catalog_url_matches_official_openai_compat_endpoint() -> None:
    spec = REGISTRY.PROVIDERS["gemini-free"]
    assert SELECTOR.catalog_url("gemini-free", spec, {}).endswith(
        "/v1beta/openai/models"
    )
