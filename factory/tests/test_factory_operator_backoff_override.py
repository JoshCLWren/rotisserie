"""Trusted operator exact-model backoff override keeps hard provider protections."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
spec = importlib.util.spec_from_file_location(
    "factory_state_operations_operator_backoff",
    ROOT / ".github/scripts/factory_state_operations.py",
)
assert spec is not None and spec.loader is not None
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

WORKER = "1327811785100"
MODEL = "hy4-flash-free"
RUNTIME = f"opencode-go/{MODEL}"
REPO = "JoshCLWren/comic-pile"
FUTURE = datetime(2099, 11, 23, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self):
        self.status = "healthy"
        self.outcome = "work_failure"
        self.cooldown_until = None
        self.provider_outcome = "control_plane_failure"
        self.provider_quota_until = None
        self.backoff_outcome = "work_failure"
        self.suspended = False

    async def fetch(self, sql, *args):
        if "FROM public.factory_model_health" in sql:
            if self.status == "retired":
                return [{"source": "opencode-free", "model": MODEL}]
            if self.status in {"unavailable", "cooling"} and self.cooldown_until:
                return [{"source": "opencode-free", "model": MODEL}]
            return []
        assert "FROM public.factory_state_kv" in sql
        return [{"payload": [{
            "source": "opencode-free",
            "model": MODEL,
            "outcome": self.backoff_outcome,
            "next_retry_at": FUTURE.isoformat(),
            "suspended": self.suspended,
        }]}]

    async def fetchrow(self, sql, *args):
        if "FROM public.factory_model_health" in sql:
            return {
                "status": self.status, "last_outcome": self.outcome,
                "cooldown_until": self.cooldown_until,
            }
        assert "FROM public.factory_provider_budget" in sql
        return {
            "last_outcome": self.provider_outcome,
            "cooldown_until": None,
            "quota_reset_at": self.provider_quota_until,
        }


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    monkeypatch.setattr(ops, "_roster",
                        lambda: {WORKER: ("opencode-free", "")})


def export(conn, requested_model="auto"):
    return asyncio.run(
        ops._export_model_exclusions(
            conn, REPO, WORKER, requested_model,
        )
    )


def test_automatic_fleet_keeps_existing_work_failure_backoff():
    assert MODEL in export(FakeConn()).get("opencode-free", [])


def test_operator_can_retry_one_live_free_go_model_after_failed_coding_work():
    assert MODEL not in export(FakeConn(), RUNTIME).get("opencode-free", [])


@pytest.mark.parametrize("status,outcome,backoff_outcome,suspended,quota", [
    ("retired", "model_retired_410", "work_failure", False, False),
    ("cooling", "provider_failure", "work_failure", False, False),
    ("healthy", "work_failure", "provider_quota", False, False),
    ("healthy", "work_failure", "work_failure", True, False),
    ("healthy", "provider_failure", "work_failure", False, False),
    ("healthy", "work_failure", "work_failure", False, True),
])
def test_exact_pin_cannot_bypass_hard_unavailability(
    status, outcome, backoff_outcome, suspended, quota,
):
    conn = FakeConn()
    conn.status, conn.outcome = status, outcome
    conn.backoff_outcome, conn.suspended = backoff_outcome, suspended
    if status == "cooling":
        conn.cooldown_until = FUTURE
    if quota:
        conn.provider_outcome = "provider_quota"
        conn.provider_quota_until = FUTURE
    assert MODEL in export(conn, RUNTIME).get("opencode-free", [])


def test_operator_override_requires_trusted_matching_worker():
    with pytest.raises(ValueError, match="trusted worker"):
        asyncio.run(ops._export_model_exclusions(
            FakeConn(), REPO, "", RUNTIME))
    with pytest.raises(ValueError, match="does not belong"):
        export(FakeConn(), "openrouter/not-the-go-model")
