"""Factory slots come from provider integrations, models from live catalogs."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
SLOTS = importlib.import_module("factory_worker_slots")


def test_no_model_roster_or_expected_worker_lock_is_committed() -> None:
    """Static roster and worker-lock files are not authoritative artifacts."""
    assert not (ROOT / ".github/free-model-factories.tsv").exists()
    assert not (ROOT / ".github/factory-expected-workers.json").exists()
    assert not (ROOT / ".github/factory-roster-snapshot.json").exists()


def test_provider_workers_have_stable_id_and_no_model_field() -> None:
    """Provider slots expose stable worker identities without model pins."""
    generated = SLOTS.slots({"FACTORY_SLOTS_PER_PROVIDER": "2"})
    assert len(generated) == len(SLOTS.sources()) * 2
    assert len({s.worker for s in generated}) == len(generated)
    assert all("model" not in s.as_record() for s in generated)


def test_dispatch_and_execution_share_dynamic_slot_registry() -> None:
    """Dispatch and execution both derive workers from the dynamic slot registry."""
    dispatcher = (ROOT / ".github/workflows/fixed-model-factory-dispatch.yml").read_text()
    runner = (ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    assert 'python3 "$slots" offer' in dispatcher
    assert "factory_worker_slots.py resolve" in runner
    assert "factory_runtime_model_select.py" in runner
    assert "factory_live_opencode.py" in runner
    assert "factory_kilo_catalog.py" in runner
    assert "Run continuous fixed-model factory session" in runner


def test_free_price_policy_is_not_a_model_selection_gate() -> None:
    """Model selection must not hard-gate on committed free-price policy text."""
    runner = (ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    assert "Require Vercel model to remain zero-price" not in runner
    assert "selected-from-live-runtime-catalog" in runner
