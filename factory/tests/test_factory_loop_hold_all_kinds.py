"""A parked or looping item gets no pool worker of any kind until its head changes or a person acts.

The 2026-11-25 ComicPile case: the sweep parked #5681 at stage changes-requested.
A failed worker then released it to factory:review, and the dispatcher offered
it again as merge-conflict work at the same head.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import factory_assign_trace as trace
import factory_dispatch_holds as holds
import factory_self_heal as heal
import factory_self_heal_rules as rules
import pytest
from factory_work_policy import Candidate, completion_action_for

from tests import turso_support as support

events = importlib.import_module("factory_turso_events")
HEAD = "aad6557624bb4d843213ced45f23273f4acd7029"
NEW_HEAD = "b5826c026697f3b9bee81a3f7992cbc7ba303eb6"


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        trace.reset()
        yield db, repo
        trace.reset()


def park(repo: str, number: int, stage: str) -> None:
    ref = f"{stage.removeprefix('factory:')}-{HEAD[:12]}"
    entry = heal.event(kind=rules.LOOP_PARKED, pr=number, head=HEAD, ref=ref, text="t", stage=stage)
    assert heal.LedgerStore(repo).record([entry]) == 1


def failures(repo: str, kind: str, number: int, count: int) -> None:
    worker = support.uncapped_workers(1)[0]
    events.record_events(
        repo,
        [
            {
                "event_type": "worker_failed",
                "event_id": f"runtime:{kind}:{number}:{n}",
                "worker": worker,
                "run_id": str(900 + n),
                "source": "nvidia",
                "model": "lab/model",
                "outcome_class": "work_failure",
                "selected_kind": kind,
                "selected_number": str(number),
                "action_kind": "implement" if kind == "issue" else "merge-conflict",
            }
            for n in range(count)
        ],
    )


def conflicted_pr(stage: str, *, override: bool = False) -> Candidate:
    return Candidate(
        "pr",
        5681,
        3,
        1,
        "2026-11-22T00:00:00Z",
        linked_issue=2407,
        stage=stage,
        conflicted=True,
        override=override,
    )


@pytest.mark.parametrize("stage", ["factory:review", "factory:changes-requested", "factory:ci"])
def test_a_pr_parked_at_changes_requested_is_held_as_merge_conflict_work(env, stage: str) -> None:
    _, repo = env
    park(repo, 5681, "factory:changes-requested")
    offered = conflicted_pr(stage)
    assert completion_action_for(offered.stage, offered.conflicted) == "merge-conflict"
    kept = holds.apply(repo, [offered], [{"number": 5681, "headRefOid": HEAD}])
    assert kept == []
    assert trace.summary() == "#5681: held: loop-parked"


def test_a_new_head_or_an_owner_override_releases_the_park(env) -> None:
    _, repo = env
    park(repo, 5681, "factory:changes-requested")
    moved = [{"number": 5681, "headRefOid": NEW_HEAD}]
    assert holds.apply(repo, [conflicted_pr("factory:review")], moved) != []
    owner = conflicted_pr("factory:review", override=True)
    assert holds.apply(repo, [owner], [{"number": 5681, "headRefOid": HEAD}]) == [owner]


def test_a_looping_issue_gets_no_new_implement_worker(env) -> None:
    _, repo = env
    issue = Candidate("issue", 5751, 3, 1, "2026-11-20T00:00:00Z")
    failures(repo, "issue", 5751, 3)
    assert holds.apply(repo, [issue], []) == [issue]
    failures(repo, "issue", 5751, 5)
    assert holds.apply(repo, [issue], []) == []
    assert trace.summary() == "#5751: held: looping"
    owner = Candidate("issue", 5751, 3, 1, "2026-11-20T00:00:00Z", override=True)
    assert holds.apply(repo, [owner], []) == [owner]


def test_a_store_failure_holds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import factory_state_operations

    def broken(*_a: object, **_k: object) -> dict[int, str]:
        raise RuntimeError("store down")

    monkeypatch.setattr(factory_state_operations, "holds", broken)
    offered = conflicted_pr("factory:review")
    assert holds.apply("acme/widgets", [offered], []) == [offered]
