"""Catalog slots need a runnable model; review PRs stop re-dispatching after aborts."""

from __future__ import annotations

import asyncio
import sys
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
        return [{"targets": targets} for targets in self.released]


def test_review_pr_cools_after_repeated_pre_session_aborts():
    heads = {
        5688: ("a" * 40, "factory:review"),
        5700: ("b" * 40, "factory:review"),
        5701: ("c" * 40, "factory:ci"),
    }
    conn = Events([[5688], [5688], [5700], [5701], [5701]])
    assert asyncio.run(operations._cooling_prs(conn, REPO, heads)) == {5688}
