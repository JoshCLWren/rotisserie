"""Catalog slots need a runnable model; review PRs stop re-dispatching after aborts."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import factory_state_operations as operations  # noqa: E402
import factory_state_operations_core as core  # noqa: E402
import factory_state_operations_slots as slots  # noqa: E402

REPO = "acme/widgets"
VERCEL = {"data": [{"id": "a/one", "type": "language"}, {"id": "b/two", "type": "language"}]}


def test_slot_is_ineligible_when_every_catalog_model_is_excluded(monkeypatch):
    monkeypatch.setattr(
        core,
        "_roster",
        lambda: {"1": ("vercel-ai-gateway", ""), "2": ("nvidia", ""), "3": ("openrouter-free", "")},
    )

    def reader(source):
        if source == "openrouter-free":
            raise RuntimeError("catalog down")
        return VERCEL

    excluded = {"vercel-ai-gateway": ["a/one", "b/two"]}
    kept, rejected = slots.drop_exhausted_slots(REPO, ["1", "2", "3"], excluded, reader)
    assert kept == ["2", "3"]
    assert rejected == ["1: every vercel-ai-gateway catalog model is excluded or cooling"]
    kept, _ = slots.drop_exhausted_slots(REPO, ["1"], {"vercel-ai-gateway": ["a/one"]}, reader)
    assert kept == ["1"]


def test_credit_pool_counts_against_priced_models_only():
    catalog = {
        "data": [
            {"id": "paid", "type": "language", "pricing": {"input": "1", "output": "2"}},
            {"id": "free", "type": "language", "pricing": {"input": "0", "output": "0"}},
        ]
    }
    pool = {"@credit_pool": ["vercel-ai-gateway"]}
    assert not slots.exhausted_sources({"vercel-ai-gateway": catalog}, pool)
    assert slots.exhausted_sources(
        {"vercel-ai-gateway": catalog},
        {**pool, "vercel-ai-gateway": ["free"]},
    ) == {"vercel-ai-gateway"}


class Events:
    def __init__(self, released):
        self.released = released

    async def fetch(self, sql, *args):
        if "factory_pr_stage_retry" in sql:
            return []
        assert "worker_released" in sql and "pre-session-abort" in sql
        return [{"targets": targets, "created_at": at} for targets, at in self.released]


def test_review_pr_cooldown_starts_at_the_latest_abort_and_grows():
    now = datetime.now(UTC)

    def ago(minutes):
        return now - timedelta(minutes=minutes)

    heads = {
        5688: ("a" * 40, "factory:review"),
        5700: ("b" * 40, "factory:review"),
        5701: ("c" * 40, "factory:ci"),
        5702: ("d" * 40, "factory:review"),
        5703: ("e" * 40, "factory:review"),
    }
    conn = Events([
        ([5688], ago(10)), ([5688], ago(5)),
        ([5700], ago(1)),
        ([5701], ago(2)), ([5701], ago(1)),
        ([5702], ago(50)), ([5702], ago(40)),
        ([5703], ago(90)), ([5703], ago(60)), (["5703"], ago(40)),
    ])
    assert asyncio.run(operations._cooling_prs(conn, REPO, heads)) == {5688, 5703}


def test_filter_keeps_workers_when_the_slot_check_fails(monkeypatch):
    monkeypatch.setattr(operations, "enabled", lambda: True)
    monkeypatch.setattr(operations, "_run", lambda fn, repo, workers: (list(workers), []))

    def broken(*_args, **_kwargs):
        raise RuntimeError("neon down")

    monkeypatch.setattr(operations, "export_model_exclusions", broken)
    kept, rejected = operations.filter_workers(REPO, ["1", "2"])
    assert kept == ["1", "2"]
    assert rejected == ["catalog slot check skipped (RuntimeError)"]
    monkeypatch.setattr(operations, "export_model_exclusions", lambda repo: {})
    monkeypatch.setattr(
        slots, "drop_exhausted_slots", lambda repo, workers, excluded: (workers[:1], ["2: empty"])
    )
    assert operations.filter_workers(REPO, ["1", "2"]) == (["1"], ["2: empty"])
