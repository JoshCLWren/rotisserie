"""Catalog slots need a runnable model; review PRs stop re-dispatching after aborts."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import factory_state_operations as operations  # noqa: E402
import factory_state_operations_slots as slots  # noqa: E402
import factory_turso_exports as exports  # noqa: E402
import factory_turso_operations as turso_operations  # noqa: E402
import pytest  # noqa: E402

from tests import turso_support as support  # noqa: E402

REPO = "acme/widgets"


@pytest.fixture
def store(tmp_path, monkeypatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


VERCEL = {"data": [{"id": "a/one", "type": "language"}, {"id": "b/two", "type": "language"}]}


def test_slot_is_ineligible_when_every_catalog_model_is_excluded(monkeypatch):
    support.use_roster(
        monkeypatch,
        {"1": ("vercel-ai-gateway", ""), "2": ("nvidia", ""), "3": ("openrouter-free", "")},
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


def _released(db, repo, released):
    statements = []
    for index, (targets, minutes_ago) in enumerate(released):
        sql, params = support.core.event_insert(
            repo,
            "github-actions",
            "worker_released",
            {
                "event_id": f"{repo}:released:{index}",
                "worker": "1",
                "targets": targets,
                "reason": "pre-session-abort: no model",
            },
            subject_type="worker",
            subject_number=1,
        )
        statements.append((sql, params))
        statements.append(
            (
                "UPDATE factory_state_events SET created_at = unixepoch() - :ago"
                " WHERE event_id = :e",
                {"ago": minutes_ago * 60, "e": f"{repo}:released:{index}"},
            )
        )
    db.batch(statements)


def test_review_pr_cooldown_starts_at_the_latest_abort_and_grows(store):
    db, repo = store
    heads = {
        5688: ("a" * 40, "factory:review"),
        5700: ("b" * 40, "factory:review"),
        5701: ("c" * 40, "factory:ci"),
        5702: ("d" * 40, "factory:review"),
        5703: ("e" * 40, "factory:review"),
    }
    _released(
        db,
        repo,
        [
            ([5688], 10),
            ([5688], 5),
            ([5700], 1),
            ([5701], 2),
            ([5701], 1),
            ([5702], 50),
            ([5702], 40),
            ([5703], 90),
            ([5703], 60),
            (["5703"], 40),
        ],
    )
    assert operations.cooling_prs(repo, heads) == {5688, 5703}


def test_filter_keeps_workers_when_the_slot_check_fails(store, monkeypatch):
    _, repo = store
    monkeypatch.setattr(turso_operations, "eligibility", lambda db, r, ws: (list(ws), []))

    def broken(*_args, **_kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(exports, "export_model_exclusions", broken)
    kept, rejected = operations.filter_workers(repo, ["1", "2"])
    assert kept == ["1", "2"]
    assert rejected == ["catalog slot check skipped (RuntimeError)"]
    monkeypatch.setattr(exports, "export_model_exclusions", lambda repo: {})
    monkeypatch.setattr(
        slots, "drop_exhausted_slots", lambda repo, workers, excluded: (workers[:1], ["2: empty"])
    )
    assert operations.filter_workers(repo, ["1", "2"]) == (["1"], ["2: empty"])
