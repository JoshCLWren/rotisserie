"""Dynamic model discovery: no permanent pin retirement or frozen roster."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
CATALOG = importlib.import_module("factory_provider_candidates")
SELECT = importlib.import_module("factory_runtime_model_select")
SLOTS = importlib.import_module("factory_worker_slots")


@pytest.mark.parametrize("source", sorted(CATALOG.ADAPTERS))
def test_provider_catalogs_never_turn_into_model_pins(source: str) -> None:
    slot = next(s for s in SLOTS.slots() if s.source == source)
    assert "model" not in slot.as_record()
    if source == "kilo-auto":
        assert CATALOG.discover(source, "ignored").status == "indeterminate"
    elif source == "opencode-free":
        parsed = CATALOG.discover(source, "opencode/synthetic-a\nopencode/synthetic-b\n")
        assert {m.model for m in parsed.candidates} == {"synthetic-a", "synthetic-b"}
    else:
        # OpenRouter's free endpoint type is the ":free" variant, not a price.
        suffix = ":free" if source == "openrouter-free" else ""
        payload = json.dumps({"data": [
            {"id": f"synthetic/a{suffix}", "pricing": {"input": 9, "output": 2}},
            {"id": f"synthetic/b{suffix}"},
        ]})
        parsed = CATALOG.discover(source, payload)
        assert {m.model for m in parsed.candidates} == {
            f"synthetic/a{suffix}", f"synthetic/b{suffix}",
        }


def test_temporary_failure_does_not_retire_a_catalog_model() -> None:
    catalog = {"data": [{"id": "synthetic/first"}, {"id": "synthetic/second"}]}
    selected = SELECT.select("nvidia", "12345", catalog, {"nvidia": ["synthetic/first"]})
    assert selected["model"] == "synthetic/second"
    selected_after_cooldown = SELECT.select("nvidia", "12345", {"data": [
        {"id": "synthetic/first"},
    ]})
    assert selected_after_cooldown["model"] == "synthetic/first"


def test_missing_catalog_is_not_implicit_consent_to_a_frozen_model() -> None:
    for empty in ({}, {"data": []}, {"data": "not a list"}):
        with pytest.raises(SELECT.CandidateUnavailableError):
            SELECT.select("nvidia", "12345", empty)


def test_published_ids_are_passed_through_unchanged() -> None:
    result = SELECT.select("groq-free", "12345", {
        "data": [{"id": "example/vendor-qualified:experimental"}],
    })
    assert result["model"] == "example/vendor-qualified:experimental"
    assert result["runtime_model"] == "groq/example/vendor-qualified:experimental"


def test_removed_roster_files_never_reappear() -> None:
    for path in (
        ".github/free-model-factories.tsv",
        ".github/factory-expected-workers.json",
        ".github/factory-roster-snapshot.json",
        ".github/scripts/factory_model_retirement.py",
        ".github/scripts/factory_roster.py",
        ".github/workflows/factory-model-discovery.yml",
    ):
        assert not (ROOT / path).exists(), path
