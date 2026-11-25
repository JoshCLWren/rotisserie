"""The breaker counts what a run did: delivered work is progress, provider crashes say nothing."""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from pathlib import Path

import factory_item_loop as loop
import pytest

from tests import turso_support as support

events = importlib.import_module("factory_turso_events")
operations = importlib.import_module("factory_turso_operations")
HEAD = "d" * 40
NOW = datetime.fromisoformat("2026-11-25T06:00:00+00:00")


def run(minutes: int, outcome: str, *, delivered: bool = False) -> loop.Run:
    return loop.Run(
        NOW - timedelta(minutes=minutes), "repair", "kimi", outcome, delivered=delivered
    )


def test_a_repair_pushed_before_the_agent_deadline_is_progress() -> None:
    rows = [run(m, "agent_timeout", delivered=True) for m in (50, 40, 30)]
    rows.append(run(20, "work_failure"))
    assert loop.assess("pr:5769", rows, now=NOW) is None


def test_an_agent_deadline_without_a_push_still_counts_as_wasted() -> None:
    rows = [run(m, "agent_timeout") for m in (50, 40, 30, 20)]
    found = loop.assess("pr:5779", rows, now=NOW)
    assert found is not None and found.reason == "agent_timeout"


@pytest.mark.parametrize(
    "outcome", ["provider_failure", "unknown_failure", "model_unavailable", "model_retired_410"]
)
def test_a_provider_or_unclassified_failure_does_not_count(outcome: str) -> None:
    rows = [run(m, outcome) for m in (50, 40, 30)]
    rows.append(run(20, "work_failure"))
    assert loop.assess("pr:5768", rows, now=NOW) is None


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def timeouts(repo: str, number: int, count: int, *, delivered: bool) -> None:
    worker = support.uncapped_workers(1)[0]
    events.record_events(
        repo,
        [
            {
                "event_type": "worker_completed" if delivered else "worker_failed",
                "event_id": f"runtime:{900 + n}",
                "worker": worker,
                "run_id": str(900 + n),
                "source": "nvidia",
                "model": "moonshotai/kimi-k3.1-pro",
                "outcome_class": "agent_timeout",
                "delivery_outcome": "persisted" if delivered else "none",
                "delivery_pr": str(number) if delivered else None,
                "persisted_work": delivered,
                "selected_kind": "pr",
                "selected_number": str(number),
                "action_kind": "review-findings",
            }
            for n in range(count)
        ],
    )


def test_the_dispatcher_does_not_hold_a_pr_whose_timed_out_runs_pushed(env) -> None:
    _, repo = env
    timeouts(repo, 5769, 5, delivered=True)
    assert operations.cooling_prs(repo, {5769: (HEAD, "factory:review")}) == set()


def test_the_dispatcher_holds_a_pr_whose_timed_out_runs_pushed_nothing(env) -> None:
    _, repo = env
    timeouts(repo, 5779, 5, delivered=False)
    assert operations.cooling_prs(repo, {5779: (HEAD, "factory:review")}) == {5779}
