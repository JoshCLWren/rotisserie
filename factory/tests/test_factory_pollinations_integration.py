"""Pollinations uses the provider's live tool-callable catalog, never a model pin."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from factory_provider_registry import PROVIDERS, compatible_config, credential_status  # noqa: E402
from factory_runtime_model_select import catalog_url, select, CandidateUnavailableError  # noqa: E402
from factory_worker_slots import slots  # noqa: E402


def test_pollinations_registry_and_secret_are_discovered():
    spec = PROVIDERS["pollinations"]
    assert spec.credential == "POLLINATIONS_API_KEY"
    assert catalog_url("pollinations", spec, {}) == (
        "https://gen.pollinations.ai/v1/models"
        "?category=text&capabilities=tool_calling&agent=false"
    )
    assert not credential_status("pollinations", {})["configured"]
    assert credential_status("pollinations", {"POLLINATIONS_API_KEY": "synthetic"})["configured"]
    config = cast(dict[str, Any], compatible_config(
        "pollinations", "synthetic-publisher/model", {"POLLINATIONS_API_KEY": "test-token-value"}
    ))
    providers = config["providers"]
    assert isinstance(providers, dict)
    pollinations = providers["pollinations"]
    assert isinstance(pollinations, dict)
    models = pollinations["models"]
    assert isinstance(models, dict)
    selected = models["synthetic-publisher/model"]
    assert isinstance(selected, dict)
    assert selected["modelID"] == "synthetic-publisher/model"
    settings = pollinations["settings"]
    assert isinstance(settings, dict)
    assert settings["baseURL"] == "https://gen.pollinations.ai/v1"
    assert "test-token-value" not in str(pollinations)
    assert any(slot.source == "pollinations" for slot in slots({"FACTORY_SLOTS_PER_PROVIDER": "1"}))


def test_select_canonical_published_model_and_honor_cooldowns():
    catalog = {"data": [
        {"id": "vendor/new", "aliases": ["old-name"], "type": "language", "supported_parameters": ["tools"]},
        {"id": "vendor/other", "type": "language", "supported_parameters": ["tools"]},
    ]}
    selection = select("pollinations", "12345", catalog, {"pollinations": ["vendor/other"]})
    assert selection["model"] == "vendor/new"
    assert selection["runtime_model"] == "pollinations/vendor/new"
    try:
        select("pollinations", "12345", catalog, {"pollinations": ["vendor/new", "vendor/other"]})
    except CandidateUnavailableError:
        pass
    else:
        raise AssertionError("all models in cooldown must not be selected")


def test_github_actions_secret_and_dynamic_provider_path():
    run = (ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    dispatch = (ROOT / ".github/workflows/fixed-model-factory-dispatch.yml").read_text()
    assert "POLLINATIONS_API_KEY: ${{ secrets.POLLINATIONS_API_KEY }}" in run
    assert "FACTORY_HAS_POLLINATIONS_API_KEY: ${{ secrets.POLLINATIONS_API_KEY != '' }}" in dispatch
    assert 'factory_runtime_model_select.py' in run
    assert 'factory_provider_registry.py config' in run
