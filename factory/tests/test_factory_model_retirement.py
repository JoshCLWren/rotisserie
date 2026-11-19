"""Model failure cooldowns expire; deletion of catalog rows is never permanent."""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
STATE = importlib.import_module("factory_state_operations")


def test_fibonacci_worker_backoff_is_durable_across_runtime_model_changes() -> None:
    now = datetime.now(UTC)
    retry_at = (now + timedelta(minutes=15)).isoformat()
    record = {"payload": [{
        "source": "nvidia", "model": "synthetic/previous",
        "next_retry_at": retry_at, "suspended": False,
    }], "expires_at": now + timedelta(days=90)}
    reason = STATE.worker_backoff_reason(record, "nvidia", "", now)
    assert reason and "Fibonacci" in reason
    assert STATE.worker_backoff_reason(record, "nvidia", "", now + timedelta(hours=1)) is None
    assert STATE.worker_backoff_reason(record, "another-provider", "", now) is None


def test_fibonacci_delay_is_uncapped_and_increases() -> None:
    delays = [STATE.fibonacci_worker_delay(n) for n in range(1, 25)]
    assert all(amount > 0 for amount in delays)
    assert delays == sorted(delays)
    assert delays[-1] > 86_400
