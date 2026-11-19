"""Provider slots, model selection and worker retry are independent concepts."""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
SLOTS = importlib.import_module("factory_worker_slots")
STATE = importlib.import_module("factory_state_operations")


def test_provider_slots_contain_no_model_inventory() -> None:
    slots = SLOTS.slots({"FACTORY_SLOTS_PER_PROVIDER": "2"})
    assert slots
    assert all("model" not in slot.as_record() for slot in slots)
    assert len({slot.worker for slot in slots}) == len(slots)
    assert {slot.source for slot in slots} == set(SLOTS.sources())


def test_worker_fibonacci_backoff_survives_model_rotation() -> None:
    now = datetime.now(UTC)
    cooldown = (now + timedelta(minutes=15)).isoformat()
    record = {
        "payload": [{
            "source": "openrouter-free",
            "model": "sample/a",
            "streak": 4,
            "next_retry_at": cooldown,
            "suspended": False,
        }],
        "expires_at": now + timedelta(days=90),
    }
    assert "Fibonacci" in (
        STATE.worker_backoff_reason(record, "openrouter-free", "", now) or ""
    )
    assert STATE.worker_backoff_reason(record, "vercel-ai-gateway", "", now) is None


def test_dispatched_model_is_looked_up_at_execution() -> None:
    workflow = (ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    assert "factory_worker_slots.py resolve" in workflow
    assert "factory_runtime_model_select.py" in workflow
    assert "factory_published_model_catalog.py" in workflow
    assert "factory_live_opencode.py" in workflow
    assert "factory_kilo_catalog.py" in workflow
    assert "steps.executor.outputs.model || steps.lane.outputs.model" in workflow


def test_historical_worker_streak_spans_model_rotations() -> None:
    def event(run_id: int, model: str, kind: str = "worker_failed") -> dict:
        return {
            "event_type": kind,
            "detail": {
                "run_id": str(run_id), "source": "openrouter-free",
                "model": model, "outcome_class": "provider_failure",
                "configured": True,
            },
        }
    events = [
        event(43, "sample/b"),
        event(42, "sample/a"),
        event(41, "sample/c"),
    ]
    assert STATE.historical_failure_streak(
        events, 44, "openrouter-free", "sample/b",
        model_only=False, model_independent=True,
    ) == 3
    assert STATE.historical_failure_streak(
        events, 44, "openrouter-free", "sample/b",
        model_only=False, model_independent=False,
    ) == 1
