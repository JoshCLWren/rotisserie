"""Provider daily quotas follow dynamic worker IDs, not model pins."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
BUDGET = importlib.import_module("factory_source_budget")
SLOTS = importlib.import_module("factory_worker_slots")


def test_dynamic_source_mapping_never_reads_a_model_manifest() -> None:
    sources = BUDGET.worker_sources()
    assert sources
    assert sources == {slot.worker: slot.source for slot in SLOTS.slots()}
    assert set(sources.values()) == set(SLOTS.sources())


def test_within_batch_quota_is_shared_by_provider() -> None:
    source_map = {"101": "synthetic-provider", "102": "synthetic-provider",
                  "103": "another-provider"}
    kept, notes = BUDGET.filter_workers(
        ["101", "102", "103"],
        titles=[], caps={"synthetic-provider": 1}, sources=source_map,
    )
    assert kept == ["101", "103"]
    assert len(notes) == 1


def test_provider_quota_cools_then_recovers_from_history() -> None:
    source_map = {"101": "synthetic-provider", "102": "another-provider"}
    title = "Factory 101 · fixed-model entry"
    kept, notes = BUDGET.filter_workers(
        ["101", "102"], titles=[title],
        caps={"synthetic-provider": 1}, sources=source_map,
    )
    assert kept == ["102"]
    assert notes
    restored, _ = BUDGET.filter_workers(
        ["101", "102"], titles=[],
        caps={"synthetic-provider": 1}, sources=source_map,
    )
    assert restored == ["101", "102"]


def test_no_fixed_worker_id_can_bypass_an_exhausted_provider_quota() -> None:
    sources = {"46": "synthetic-provider"}
    kept, _ = BUDGET.filter_workers(
        ["46"], titles=["Factory 46 · fixed-model entry"],
        caps={"synthetic-provider": 1}, sources=sources,
    )
    assert kept == []


def test_unmetered_provider_is_not_dropped_for_another_source_quota() -> None:
    sources = {"101": "synthetic-provider", "102": "another-provider"}
    kept, _ = BUDGET.filter_workers(
        ["101", "102"], titles=["Factory 101 · fixed-model entry"],
        caps={"synthetic-provider": 1}, sources=sources,
    )
    assert kept == ["102"]


def test_budget_guard_uses_recent_actions_evidence_not_model_inventory() -> None:
    assert BUDGET.HARD_FREE_SOURCE_DISPATCH_CAPS["cloudflare-workers-ai"] > 0
    assert BUDGET.PROTECTED_WORKERS == frozenset()
    registry = (ROOT / ".github/scripts/factory_source_budget.py").read_text()
    assert "free-model-factories.tsv" not in registry


def test_dispatch_filters_provider_budget_before_batch_cap() -> None:
    text = (ROOT / ".github/workflows/fixed-model-factory-dispatch.yml").read_text()
    step = text[text.index('workers="$(python3 "$slots" offer'):]
    assert step.index("factory_source_budget.py filter") < step.index("'.[:$cap]'")


def test_unknown_source_has_no_automatic_licensed_model() -> None:
    source_map = {"101": "another-provider"}
    kept, notes = BUDGET.filter_workers(
        ["101"], titles=[], caps={"synthetic-provider": 1}, sources=source_map,
    )
    assert kept == ["101"]
    assert notes == []
