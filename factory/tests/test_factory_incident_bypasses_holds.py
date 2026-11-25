"""Incident work is owner-directed, so the loop breaker never holds it.

The 2026-11-25 ComicPile case: incident #5746 had PR #5772 on a checkpoint
branch ("Refs #5746"). The PR looped, the breaker held it, and the swarm could
not work it until the owner added ``factory:override`` by hand.
"""

from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path
from typing import Any

import factory_assign_trace as trace
import factory_dispatch_holds as holds
import factory_self_heal as heal
import factory_self_heal_loops as sweep_loops
import factory_self_heal_rules as rules
import pytest
from factory_work_policy import LOOP_HOLD_KEY, build_candidates, factory_parked_count
from tests.test_factory_item_loop import Hub, Store

from tests import turso_support as support

events = importlib.import_module("factory_turso_events")
HEAD = "aad6557624bb4d843213ced45f23273f4acd7029"
INCIDENT = 5746
INCIDENTS = {INCIDENT: 3}


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        trace.reset()
        yield db, repo
        trace.reset()


def incident_issue() -> dict[str, object]:
    return {
        "number": INCIDENT,
        "state": "OPEN",
        "title": "Roll faces disappear",
        "labels": [{"name": n} for n in ("factory", "factory:unowned", "factory:incident")],
        "createdAt": "2026-11-24T00:00:00Z",
    }


def checkpoint_pr() -> dict[str, object]:
    return {
        "number": 5772,
        "state": "OPEN",
        "isDraft": False,
        "title": "P1 production incident: Roll faces disappear",
        "body": f"Refs #{INCIDENT}. Keep open until exact-head CI.",
        "labels": [{"name": n} for n in ("factory", "factory:unowned", "factory:review")],
        "headRefName": f"factory/work-checkpoints/issue-{INCIDENT}/iceman/45578987720",
        "headRefOid": HEAD,
        "createdAt": "2026-11-25T00:00:00Z",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }


def other_pr() -> dict[str, object]:
    return {
        **checkpoint_pr(),
        "number": 5681,
        "title": "Queue covers",
        "body": "Worker: opencode-free-model-factory-20",
        "headRefName": "factory/20-2407-queue-covers",
    }


def park(repo: str, number: int) -> None:
    ref = f"review-{HEAD[:12]}"
    entry = heal.event(
        kind=rules.LOOP_PARKED, pr=number, head=HEAD, ref=ref, text="t", stage="factory:review"
    )
    assert heal.LedgerStore(repo).record([entry]) == 1


def failures(repo: str, kind: str, number: int, count: int = 5) -> None:
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
                "action_kind": "implement" if kind == "issue" else "review",
            }
            for n in range(count)
        ],
    )


def candidates(prs: list[dict[str, object]]) -> list[Any]:
    found = build_candidates([incident_issue()], prs, incidents=INCIDENTS)
    return [c for c in found if c.kind == "pr"]


def test_the_checkpoint_pr_of_an_incident_ranks_as_incident_work() -> None:
    found = {c.number: c for c in candidates([checkpoint_pr(), other_pr()])}
    assert found[5772].incident is True
    assert found[5681].incident is False


def test_a_parked_incident_pr_is_not_held_but_an_unrelated_one_is(env) -> None:
    _, repo = env
    park(repo, 5772)
    park(repo, 5681)
    prs = [checkpoint_pr(), other_pr()]
    kept = holds.apply(repo, candidates(prs), prs)
    assert [c.number for c in kept] == [5772]
    assert trace.summary() == "#5681: held: loop-parked"


def test_a_looping_incident_pr_and_issue_are_not_held(env) -> None:
    _, repo = env
    failures(repo, "pr", 5772)
    failures(repo, "pr", 5681)
    failures(repo, "issue", INCIDENT)
    prs = [checkpoint_pr(), other_pr()]
    offered = build_candidates([incident_issue()], prs, incidents=INCIDENTS)
    kept = {c.number for c in holds.apply(repo, offered, prs)}
    assert {5772, INCIDENT} <= kept
    assert 5681 not in kept


def test_stamp_leaves_incident_prs_out_of_the_parked_count(env) -> None:
    _, repo = env
    park(repo, 5772)
    park(repo, 5681)
    stamped = holds.stamp(repo, [checkpoint_pr(), other_pr()], [incident_issue()], INCIDENTS)
    marks = {int(str(row["number"])): row.get(LOOP_HOLD_KEY) for row in stamped}
    assert marks == {5772: None, 5681: "held: loop-parked"}
    assert factory_parked_count(stamped) == 1


class IncidentHub(Hub):
    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if path == f"issues/{INCIDENT}":
            return {"number": INCIDENT, "state": "open", "labels": [{"name": "factory:incident"}]}
        return super().api(path, *args, payload=payload)


def rest_pull(head_ref: str) -> dict[str, Any]:
    names = ["factory", "factory:unowned", "factory:review"]
    return {
        "number": 5766,
        "state": "open",
        "title": "Normalize the API key at one boundary",
        "body": f"Refs #{INCIDENT}.",
        "head": {"sha": HEAD, "ref": head_ref},
        "labels": [{"name": name} for name in names],
    }


def run_sweep(monkeypatch: pytest.MonkeyPatch, head_ref: str) -> tuple[Hub, Store, list[Any]]:
    import factory_self_heal_actions as actions

    monkeypatch.delenv("FACTORY_REPO_VARS", raising=False)
    monkeypatch.setenv("FACTORY_INCIDENTS", f"{INCIDENT}:3")
    hub, store = IncidentHub(), Store()
    found = sweep_loops.park_loops(
        hub,
        store,
        [rest_pull(head_ref)],
        actions.Budget(actions=10, syncs=0),
        now=datetime.fromisoformat("2026-11-25T01:30:00+00:00"),
    )
    return hub, store, found


def test_the_sweep_does_not_park_an_incident_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    hub, store, found = run_sweep(monkeypatch, f"factory/work-checkpoints/issue-{INCIDENT}/m/1")
    assert [(row["pr"], row["status"]) for row in found] == [(5766, "incident")]
    assert hub.writes == []
    assert store.events == []


def test_the_sweep_still_parks_an_unrelated_looping_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    hub, store, found = run_sweep(monkeypatch, "factory/20-2407-queue-covers")
    assert [row["pr"] for row in found] == [5766]
    assert store.events[0]["kind"] == rules.LOOP_PARKED
    assert hub.writes[0][0] == "issues/5766/comments"
