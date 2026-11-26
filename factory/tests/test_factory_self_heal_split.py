"""The sweep dispatches split runs, creates children from a recorded plan, and tracks the parent."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import factory_model_track_record as track
import factory_self_heal as heal
import factory_self_heal_split as split
import factory_split_plan as plans
import factory_state_ledger as ledger
import pytest
from factory_work_policy_base import eligibility
from tests.test_factory_item_loop import heal_budget
from tests.test_factory_split_plan import PARENT, raw

from tests import turso_support as support

BIG = "x" * 17_000


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stamp = datetime.now(UTC).isoformat()
    stats = tmp_path / "stats.json"
    window = {"completed": 9, "failed": 1, "delivered": 9}
    stats.write_text(
        json.dumps(
            {
                "generated_at": stamp,
                "counts_generated_at": stamp,
                "models": [
                    {"source": "opencode-free", "model": "planner", "windows": {"7d": window}}
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(track.FILE_ENV, str(stats))
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


class Hub:
    def __init__(
        self,
        repo: str,
        held: list[dict[str, Any]] | None = None,
        states: dict[int, tuple[str, str]] | None = None,
        fail_at: int = 0,
    ) -> None:
        self.repo = repo
        self.writes: list[tuple[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.held = held or []
        self.states = states or {}
        self.fail_at = fail_at
        self.posted: dict[int, list[dict[str, Any]]] = {}

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if path.startswith("issues?labels="):
            wanted = set(unquote(path.split("=", 1)[1].split("&", 1)[0]).split(","))
            return [row for row in self.held if wanted <= {x["name"] for x in row["labels"]}]
        if path.startswith("issues?creator="):
            return [{"number": row["number"], "body": row["body"]} for row in self.created]
        if path == "issues" and payload is not None:
            if self.fail_at and len(self.created) + 1 == self.fail_at:
                self.fail_at = 0
                self.created.append({"number": 5000 + len(self.created) + 1, **payload})
                raise RuntimeError("HTTP 502")
            number = 5000 + len(self.created) + 1
            self.created.append({"number": number, **payload})  # type: ignore[dict-item]
            return {"number": number}
        self.writes.append((path, payload))
        if path.endswith("/comments") and isinstance(payload, dict):
            number = int(path.split("/")[1])
            self.posted.setdefault(number, []).append(payload)
        return {}

    def comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.posted.get(number, []))

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        rows = {
            f"c{n}": {"state": state, "stateReason": reason}
            for n, (state, reason) in self.states.items()
        }
        return {"data": {"repository": rows}}


def record_plan(repo: str) -> dict[str, Any]:
    plan = plans.validate(raw(), PARENT)
    assert ledger.record_events(repo, [plans.plan_event(plan, "1")]) == 1
    return plan


def sweep(repo: str, hub: Hub) -> list[dict[str, Any]]:
    return split.advance_splits(hub, heal.LedgerStore(repo), [], heal_budget())


def held(number: int, body: str = BIG, *labels: str) -> dict[str, Any]:
    names = ["factory", "factory:unowned", "factory:needs-split", *labels]
    return {
        "number": number,
        "state": "open",
        "body": body,
        "labels": [{"name": name} for name in names],
    }


def test_children_are_created_in_blocker_order_and_the_parent_tracks_them(env) -> None:
    _, repo = env
    record_plan(repo)
    hub = Hub(repo)
    found = sweep(repo, hub)
    assert [row["title"] for row in hub.created] == ["Reader", "Schema", "Writer"]
    reader, schema, writer = (row["number"] for row in hub.created)
    assert all(row["labels"] == ["factory", "factory:unowned"] for row in hub.created)
    assert eligibility.parse_declared_dependencies(hub.created[1]["body"]) == {reader}
    assert eligibility.parse_declared_dependencies(hub.created[2]["body"]) == {reader, schema}
    assert found[0] == {
        "issue": PARENT,
        "status": "split done",
        "children": [reader, schema, writer],
    }
    assert (f"issues/{PARENT}/labels", {"labels": ["tracking"]}) in hub.writes
    checklist = str(hub.posted[PARENT][0]["body"])
    assert split.done_marker(PARENT) in checklist and f"- [ ] #{writer} Writer" in checklist
    import factory_deps as deps

    links = {(link.src, link.dst, link.kind) for link in deps.read_links(repo)}
    assert links == {
        (PARENT, reader, "parent-child"), (PARENT, schema, "parent-child"),
        (PARENT, writer, "parent-child"), (reader, schema, "blocks"), (reader, writer, "blocks"),
        (schema, writer, "blocks"),
    }
    again = Hub(repo)
    assert sweep(repo, again) == [] and again.created == []


def test_a_failed_pass_resumes_the_same_plan_and_adopts_an_unrecorded_child(env) -> None:
    _, repo = env
    record_plan(repo)
    hub = Hub(repo, fail_at=2)
    with pytest.raises(RuntimeError):
        sweep(repo, hub)
    assert len(hub.created) == 2
    sweep(repo, hub)
    assert [row["title"] for row in hub.created] == ["Reader", "Schema", "Writer"]
    assert len({row["number"] for row in hub.created}) == 3


def test_a_held_issue_gets_one_split_run_on_the_best_model_and_never_a_second_plan(env) -> None:
    _, repo = env
    hub = Hub(repo, held=[held(PARENT)])
    found = sweep(repo, hub)
    dispatch = [p for path, p in hub.writes if path.endswith("/dispatches")]
    assert dispatch == [
        {
            "ref": "main",
            "inputs": {
                "mode": "smoke",
                "action": "advance",
                "provider": "opencode-free",
                "model": "opencode-go/planner",
                "force_slot": "true",
                "target": f"split:{PARENT}",
            },
        }
    ]
    assert found[0]["status"] == "split run dispatched"
    soon = Hub(repo, held=[held(PARENT)])
    assert sweep(repo, soon) == []
    later = Hub(repo, held=[held(PARENT)])
    split.advance_splits(
        later, heal.LedgerStore(repo), [], heal_budget(), now=datetime.now(UTC) + timedelta(hours=7)
    )
    assert any(path.endswith("/dispatches") for path, _ in later.writes)
    record_plan(repo)
    planned = Hub(repo, held=[held(PARENT)])
    split.advance_splits(
        planned,
        heal.LedgerStore(repo),
        [],
        heal_budget(),
        now=datetime.now(UTC) + timedelta(hours=14),
    )
    assert not any(path.endswith("/dispatches") for path, _ in planned.writes)


def started(hub: Hub, worker: str, end: str = "") -> None:
    from factory_adopter import MARKER_PREFIX
    from tests.test_factory_split_plan import claim

    rows = hub.posted.setdefault(PARENT, [])
    rows.append(claim(worker, "split"))
    if end:
        body = (
            f"<!-- {MARKER_PREFIX}-factory-claim-released-v3:issue-{PARENT}:"
            f"opencode-free-model-factory-{worker}:1764100100:split-plan-{end} -->"
        )
        rows.append({**claim(worker, "split"), "body": body})


def test_a_rejected_plan_is_retried_on_the_next_pass_and_a_silent_run_waits(env) -> None:
    _, repo = env
    hub = Hub(repo, held=[held(PARENT)])
    assert sweep(repo, hub)[0]["status"] == "split run dispatched"
    started(hub, "7")
    hub.writes.clear()
    assert sweep(repo, hub) == []
    rejected = {
        "event_type": split.EVENT,
        "event_id": f"split:rejected:{PARENT}:1",
        "worker": "0",
        "kind": "rejected",
        "issue": PARENT,
        "reason": "child 3 needs 1 to 30 files",
    }
    assert ledger.record_events(repo, [rejected]) == 1
    assert sweep(repo, hub)[0]["status"] == "split run dispatched"


def test_an_old_rejected_release_marker_also_retries_at_once(env) -> None:
    _, repo = env
    hub = Hub(repo, held=[held(PARENT)])
    assert sweep(repo, hub)[0]["status"] == "split run dispatched"
    started(hub, "7", "rejected")
    assert sweep(repo, hub)[0]["status"] == "split run dispatched"


def test_a_deferred_dispatch_is_sent_again_and_never_counts_as_a_run(env) -> None:
    _, repo = env
    hub = Hub(repo, held=[held(PARENT)])
    store = heal.LedgerStore(repo)
    now = datetime.now(UTC)
    assert sweep(repo, hub)[0]["status"] == "split run dispatched"
    soon = now + timedelta(seconds=split.START_SECONDS - 60)
    assert split.advance_splits(hub, store, [], heal_budget(), now=soon) == []
    for n in range(1, split.MAX_DISPATCHES + 2):
        later = now + timedelta(seconds=n * (split.START_SECONDS + 60))
        found = split.advance_splits(hub, store, [], heal_budget(), now=later)
        assert found[0]["status"] == "split run dispatched"
    for worker in ("7", "8", "9"):
        started(hub, worker, "handoff")
    far = now + timedelta(days=2)
    assert split.advance_splits(hub, store, [], heal_budget(), now=far) == []


def test_a_child_and_an_override_are_never_split(env) -> None:
    _, repo = env
    child_body = plans.child_marker(PARENT, 1) + "\n" + BIG
    hub = Hub(repo, held=[held(7, child_body), held(8, BIG, "factory:override")])
    assert sweep(repo, hub) == []
    assert hub.writes == []


def test_the_parent_closes_only_when_every_child_completed(env) -> None:
    _, repo = env
    record_plan(repo)
    sweep(repo, Hub(repo))
    children = (5001, 5002, 5003)
    waiting = Hub(repo, states={n: ("OPEN", "") for n in children})
    assert sweep(repo, waiting) == [] and waiting.writes == []
    finished = Hub(repo, states={n: ("CLOSED", "COMPLETED") for n in children})
    found = sweep(repo, finished)
    assert found == [{"issue": PARENT, "status": "closed: every child completed"}]
    assert (f"issues/{PARENT}", {"state": "closed", "state_reason": "completed"}) in finished.writes


def test_closing_a_child_as_not_planned_undoes_the_split_and_keeps_the_parent(env) -> None:
    _, repo = env
    record_plan(repo)
    sweep(repo, Hub(repo))
    states = {
        5001: ("CLOSED", "NOT_PLANNED"),
        5002: ("CLOSED", "COMPLETED"),
        5003: ("CLOSED", "NOT_PLANNED"),
    }
    hub = Hub(repo, states=states)
    assert sweep(repo, hub)[0]["status"].startswith("split undone")
    assert not any(path == f"issues/{PARENT}" for path, _ in hub.writes)
    assert sweep(repo, Hub(repo, states=states)) == []


def test_an_umbrella_issue_with_a_merged_slice_is_split(env) -> None:
    _, repo = env
    names = ["factory", "factory:unowned", "factory:ready", "umbrella"]
    ready = {"number": PARENT, "state": "open", "body": "Migrate every consumer.",
             "labels": [{"name": name} for name in names]}
    hub = Hub(repo, held=[ready])
    assert sweep(repo, hub)[0]["status"] == "split run dispatched"


@pytest.mark.parametrize(
    ("extra", "body"),
    [("epic", "Big."), ("umbrella", "- [ ] #12 an existing child"), ("factory:7", "Big.")],
    ids=["epic", "already lists children", "owned by a worker"],
)
def test_epics_listed_children_and_owned_issues_are_not_split(env, extra, body) -> None:
    _, repo = env
    names = ["factory", "factory:needs-split", "umbrella", extra]
    issue = {"number": PARENT, "state": "open", "body": body,
             "labels": [{"name": name} for name in names]}
    hub = Hub(repo, held=[issue])
    assert sweep(repo, hub) == [] and hub.writes == []
