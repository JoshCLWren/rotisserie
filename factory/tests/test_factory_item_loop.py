"""An item that keeps failing is called looping, held by the dispatcher and parked for the owner."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import factory_item_loop as loop
import factory_self_heal as heal
import factory_self_heal_loops as loops
import factory_self_heal_rules as rules
import pytest

from tests import turso_support as support

events = importlib.import_module("factory_turso_events")
operations = importlib.import_module("factory_turso_operations")
FIXTURE = Path(__file__).parent / "fixtures" / "item_loops_2026_11_24.json"
HEAD = "d" * 40


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def recorded() -> dict[str, list[loop.Run]]:
    found: dict[str, list[loop.Run]] = {}
    for row in json.loads(FIXTURE.read_text())["runs"]:
        found.setdefault(row["target"], []).append(
            loop.Run(moment(row["finished_at"]), row["kind"], row["model"], row["outcome"])
        )
    return found


def at(text: str, **open_items: list[str]) -> dict[str, loop.Loop]:
    items = open_items or {t: ["factory", "factory:review"] for t in recorded()}
    return {
        found.target: found for found in loop.find(recorded(), now=moment(text), open_items=items)
    }


def test_the_owner_issue_pr_was_looping_at_0130() -> None:
    found = at("2026-11-25T01:30:00Z")["pr:5766"]
    assert (found.wasted, len(found.runs)) == (6, 7)
    assert found.reason == "work_failure"
    assert found.text() == (
        "6 of 7 worker runs in the last 6h changed nothing; most often work_failure (5)."
    )
    assert "baste run --target pr:5766" in found.next_step()


def test_repeated_conflict_repairs_loop_too() -> None:
    early = at("2026-11-25T01:30:00Z")
    assert early["pr:5674"].reason == "work_failure"
    assert "pr:5681" not in early
    late = at("2026-11-25T02:30:00Z")
    assert {run.kind for run in late["pr:5681"].runs} >= {"merge-conflict"}
    assert late["pr:5681"].wasted == 4


def test_reviews_that_end_with_no_work_loop_although_github_says_success() -> None:
    assert "pr:5737" not in at("2026-11-25T01:30:00Z")
    found = at("2026-11-25T02:00:00Z")["pr:5737"]
    assert (found.wasted, len(found.runs), found.reason) == (4, 5, "no_work")


def test_a_healthy_two_round_review_is_not_a_loop() -> None:
    now = moment("2026-11-25T02:00:00Z")
    runs = [
        loop.Run(now - timedelta(minutes=50 - 10 * i), kind, "m", "success")
        for i, kind in enumerate(
            ("review", "review-findings", "review", "review-findings", "review")
        )
    ]
    assert loop.assess("pr:1", runs, now=now) is None


def test_capacity_misses_do_not_count_and_old_runs_age_out() -> None:
    now = moment("2026-11-25T02:00:00Z")
    runs = [loop.Run(now - timedelta(minutes=i), "review", "m", "no_capacity") for i in range(9)]
    runs += [
        loop.Run(now - timedelta(hours=7, minutes=i), "review", "m", "work_failure")
        for i in range(9)
    ]
    assert loop.assess("pr:1", runs, now=now) is None


def test_ready_closed_and_parked_items_start_fresh() -> None:
    assert "pr:5766" not in at("2026-11-25T01:30:00Z", **{"pr:5766": ["factory", "factory:ready"]})
    assert "pr:5766" not in at("2026-11-25T01:30:00Z", **{"pr:9": ["factory"]})
    parked = loop.find(
        recorded(),
        now=moment("2026-11-25T01:30:00Z"),
        open_items={"pr:5766": ["factory"]},
        parked_at={"pr:5766": moment("2026-11-25T01:00:00Z")},
    )
    assert parked == []


def test_owner_override_and_claim_bypass_the_breaker() -> None:
    assert loop.bypassed({"factory", "factory:override"})
    assert loop.bypassed({"factory:claimed"})
    assert not loop.bypassed({"factory", "factory:review", "factory:123"})


def test_the_park_kind_is_a_self_heal_kind() -> None:
    assert loop.PARK_KIND == rules.LOOP_PARKED
    line = rules.marker(kind=rules.LOOP_PARKED, pr=5766, head=HEAD, ref=f"review-{HEAD[:12]}")
    assert rules.parse_marker(line)["kind"] == "loop-parked"


# Self-heal sweep ----------------------------------------------------------------


class Hub:
    repo = "acme/widgets"

    def __init__(self) -> None:
        self.writes: list[tuple[str, Any]] = []

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        self.writes.append((path, payload))
        return {}


class Store:
    def __init__(self, parks: dict[str, dict[str, Any]] | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.parks = parks or {}

    def enabled(self) -> bool:
        return True

    def record(self, entries: list[dict[str, Any]]) -> int:
        self.events += entries
        return len(entries)

    def holder(self, number: int) -> str | None:
        return None

    def loop_evidence(self, seconds: int) -> tuple[dict[str, list[loop.Run]], dict[str, Any]]:
        return recorded(), self.parks


def pull(number: int, *labels: str) -> dict[str, Any]:
    names = ["factory", "factory:unowned", "factory:review", *labels]
    return {
        "number": number,
        "title": "Normalize the API key at one boundary",
        "head": {"sha": HEAD},
        "labels": [{"name": name} for name in names],
    }


def sweep(store: Store, *pulls: dict[str, Any]) -> tuple[Hub, list[dict[str, Any]]]:
    hub = Hub()
    found = loops.park_loops(
        hub,
        store,
        list(pulls),
        heal_budget(),
        now=moment("2026-11-25T01:30:00Z"),
    )
    return hub, found


def heal_budget() -> Any:
    import factory_self_heal_actions as actions

    return actions.Budget(actions=10, syncs=0)


def test_the_sweep_parks_a_looping_pr_with_a_comment_and_an_event() -> None:
    store = Store()
    hub, found = sweep(store, pull(5766))
    assert [row["pr"] for row in found] == [5766]
    path, body = hub.writes[0]
    assert path == "issues/5766/comments"
    text = str(body["body"])  # type: ignore[index]
    assert rules.parse_marker(text.splitlines()[0])["kind"] == rules.LOOP_PARKED
    assert "Parked Normalize the API key at one boundary (#5766): 6 of 7 worker runs" in text
    assert "closes it as a failed attempt" in text
    event = store.events[0]
    assert (event["kind"], event["stage"], event["head"]) == ("loop-parked", "factory:review", HEAD)
    assert event["loop"]["reason"] == "work_failure"


def test_the_sweep_parks_once_per_head_in_any_stage() -> None:
    park = {"at": moment("2026-11-25T00:00:00Z"), "at_heads": {(HEAD, "factory:changes-requested")}}
    hub, found = sweep(Store({"pr:5766": park}), pull(5766))
    assert hub.writes == [] and found == []


def test_a_repeated_or_failed_park_never_posts_a_second_comment() -> None:
    class Seen(Store):
        def record(self, entries: list[dict[str, Any]]) -> int:
            return 0

    hub, found = sweep(Seen(), pull(5766))
    assert hub.writes == [] and found == []


def test_an_owner_override_is_never_parked() -> None:
    hub, found = sweep(Store(), pull(5766, "factory:override"))
    assert hub.writes == []
    assert found[0]["status"] == "owner"


def test_the_sweep_runs_the_park_step(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()

    class Lister(Hub):
        def api(self, path: str, *args: str, payload: object | None = None) -> Any:
            if path.startswith("pulls?state=open"):
                return [[pull(5766)]]
            return super().api(path, *args, payload=payload)

        def comments(self, number: int) -> list[dict[str, Any]]:
            return []

        def graphql(self, query: str, **variables: object) -> dict[str, Any]:
            page = {"pageInfo": {"hasNextPage": False}, "nodes": []}
            return {"data": {"repository": {"pullRequests": page}}}

    park = loops.park_loops
    monkeypatch.setattr(
        loops,
        "park_loops",
        lambda *a, **k: park(*a, **{**k, "now": moment("2026-11-25T01:30:00Z")}),
    )
    result = heal.sweep(Lister(), store, dry_run=True)
    assert result["loops"][0]["status"] == "would-park"


# Dispatcher hold on the store ----------------------------------------------------


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def failures(repo: str, number: int, count: int) -> None:
    worker = support.uncapped_workers(1)[0]
    events.record_events(
        repo,
        [
            {
                "event_type": "worker_failed",
                "event_id": f"runtime:{700 + n}",
                "worker": worker,
                "run_id": str(700 + n),
                "source": "nvidia",
                "model": "lab/model",
                "outcome_class": "work_failure",
                "selected_kind": "pr",
                "selected_number": str(number),
                "action_kind": "review",
            }
            for n in range(count)
        ],
    )


def test_the_dispatcher_holds_a_looping_pr_unless_the_owner_overrides(env) -> None:
    _, repo = env
    heads = {12: (HEAD, "factory:review"), 13: (HEAD, "factory:review")}
    failures(repo, 12, 3)
    assert operations.cooling_prs(repo, heads) == set()
    failures(repo, 12, 5)
    assert operations.cooling_prs(repo, heads) == {12}
    assert operations.cooling_prs(repo, heads, frozenset({12})) == set()


def test_a_park_holds_its_head_in_any_stage(env) -> None:
    _, repo = env
    failures(repo, 12, 5)
    heal.LedgerStore(repo).record(
        [
            heal.event(
                kind=rules.LOOP_PARKED,
                pr=12,
                head=HEAD,
                ref="review-x",
                text="t",
                stage="factory:review",
            )
        ]
    )
    assert operations.cooling_prs(repo, {12: (HEAD, "factory:review")}) == {12}
    assert operations.cooling_prs(repo, {12: ("e" * 40, "factory:review")}) == set()
    assert operations.cooling_prs(repo, {12: (HEAD, "factory:changes-requested")}) == {12}


def controller_with(
    monkeypatch: pytest.MonkeyPatch, cooling: Any
) -> tuple[Any, list[Any]]:
    import importlib.util
    import sys

    import factory_state_operations

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "factory_work_controller_loop_hold", root / ".github/scripts/factory-work-controller.py"
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    looping = controller.Candidate("pr", 12, 1, 4, "2026-11-20T00:00:00Z", stage="factory:review")
    owner = controller.Candidate(
        "pr", 13, 1, 4, "2026-11-21T00:00:00Z", stage="factory:review", override=True
    )
    prs = [{"number": n, "headRefOid": HEAD} for n in (12, 13)]
    picked: list[Any] = []
    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(controller, "reconcile_contradictory_labels", lambda: [])
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: prs)
    monkeypatch.setattr(controller, "load_no_diff_attempt_records", lambda **_: [])
    monkeypatch.setattr(controller, "build_candidates", lambda *_a, **_k: [looping, owner])
    monkeypatch.setattr(controller, "rotisserie_issue_intake", lambda *_a, **_k: None)
    monkeypatch.setattr(controller, "resolve_review_head_contributors", lambda rows, _p: rows)
    monkeypatch.setattr(controller, "order_candidates_for_worker", lambda rows, _w: rows)
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _: True)
    monkeypatch.setattr(controller, "issue_excludes_worker_on_strike_retry", lambda *_: False)
    monkeypatch.setattr(
        controller, "assign_candidate", lambda c, *_a, **_k: picked.append(c) or True
    )
    monkeypatch.setattr(factory_state_operations, "holds", cooling)
    monkeypatch.setenv("FACTORY_JEV_RANKING_FILE", "")
    return controller, picked


def test_the_controller_skips_a_looping_pr_and_keeps_the_owner_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def cooling(repo: str, heads: Any, issues: Any, exempt: frozenset[int]) -> dict[int, str]:
        seen["exempt"] = exempt
        return dict.fromkeys({12, 13} - exempt, "held: looping")

    controller, picked = controller_with(monkeypatch, cooling)
    chosen = controller.assign_from_pool("6")
    assert seen["exempt"] == frozenset({13})
    assert chosen.number == 13 and [c.number for c in picked] == [13]


def test_a_targeted_run_is_never_held(monkeypatch: pytest.MonkeyPatch) -> None:
    def cooling(*_a: Any, **_k: Any) -> set[int]:
        raise AssertionError("a targeted run must not read the breaker")

    controller, picked = controller_with(monkeypatch, cooling)
    controller.assign_from_pool("6", target=("pr", 12))
    assert [c.number for c in picked] == [12]
