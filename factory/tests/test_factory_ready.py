"""The ready pool: one ranked list for the dispatcher, ``ready`` and ``baste next``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import factory_deps as deps
import factory_ready as ready
import factory_state_operations
import pytest


def issue(number: int, *labels: str, body: str = "") -> dict[str, Any]:
    return {
        "number": number,
        "state": "OPEN",
        "title": f"Issue {number}",
        "body": body,
        "labels": [{"name": name} for name in ("factory", *labels)],
        "createdAt": f"2026-11-{number:02d}T00:00:00Z",
    }


ISSUES = [
    issue(1),
    issue(2),
    issue(3, "priority:high"),
    issue(4),
    issue(5, body="Blocked by #1"),
    issue(6),
    issue(7),
]
LINKS = [deps.Link(1, 2, "blocks"), deps.Link(2, 6, "parent-child"), deps.Link(9, 7, "related")]


def numbers(found: ready.Ready) -> list[int]:
    return [candidate.number for candidate in found.candidates]


def test_the_pool_holds_blocked_issues_and_names_why() -> None:
    found = ready.pool(ISSUES, [], attempt_records=[], links=LINKS)
    assert 2 not in numbers(found) and 5 not in numbers(found) and 6 not in numbers(found)
    assert {1, 3, 4, 7} <= set(numbers(found))
    assert found.held["issue:2"] == "blocked by open #1, #6"
    assert found.held["issue:5"] == "blocked by open #1"
    assert found.held["issue:6"] == "blocked by open #1"


def test_cycle_members_are_held_and_reported() -> None:
    cycle = [deps.Link(3, 4, "blocks"), deps.Link(4, 3, "blocks")]
    found = ready.pool(ISSUES, [], attempt_records=[], links=cycle)
    assert found.cycles == ((3, 4),)
    assert found.held["issue:3"].startswith("in a dependency cycle")
    assert 3 not in numbers(found) and 4 not in numbers(found)


def test_intake_unknown_history_and_store_holds_apply_in_rank_order() -> None:
    found = ready.pool(
        ISSUES, [], attempt_records=[], intake={1, 3, 4, 7}, holds=lambda _c: {4: "held: cooling"}
    )
    assert numbers(found) == [3, 1, 7]
    assert found.held["issue:2"] == "not admitted by Rotisserie intake"
    assert found.held["issue:4"] == "held: cooling"
    assert numbers(ready.pool(ISSUES, [], attempt_records=None)) == []


def test_rows_carry_only_number_title_stage_and_action() -> None:
    found = ready.pool(ISSUES, [], attempt_records=[], links=LINKS)
    top = ready.rows(found, ISSUES, [], 2)
    assert top[0] == {
        "rank": 1,
        "target": "issue:3",
        "title": "Issue 3",
        "stage": "new",
        "action": "implement",
        "lane": top[0]["lane"],
        "priority": top[0]["priority"],
    }
    assert len(top) == 2


def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "factory_work_controller_ready", root / ".github/scripts/factory-work-controller.py"
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    return controller


def test_ready_prints_the_exact_list_the_dispatcher_walks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    controller = controller_module(monkeypatch)
    walked: list[int] = []

    def order(rows: list[Any], _worker: str) -> list[Any]:
        walked.extend(candidate.number for candidate in rows)
        return rows

    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(controller, "reconcile_contradictory_labels", lambda: [])
    monkeypatch.setattr(controller, "list_issues", lambda: ISSUES)
    monkeypatch.setattr(controller, "list_prs", lambda: [])
    monkeypatch.setattr(controller, "load_no_diff_attempt_records", lambda **_: [])
    monkeypatch.setattr(controller, "rotisserie_issue_intake", lambda *_a, **_k: None)
    monkeypatch.setattr(controller, "resolve_review_head_contributors", lambda rows, _p: rows)
    monkeypatch.setattr(controller, "order_candidates_for_worker", order)
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _c: False)
    monkeypatch.setattr(controller.factory_issue_intake_guard, "request_splits_gh", lambda *_: None)
    monkeypatch.setattr(deps, "stored", lambda _repo: LINKS)
    monkeypatch.setattr(factory_state_operations, "holds", lambda *_a: {4: "held: cooling"})
    monkeypatch.setenv("FACTORY_JEV_RANKING_FILE", "")
    assert controller.assign_from_pool("6", batch_reconciled=True) is None
    monkeypatch.setattr(sys, "argv", ["factory-work-controller.py", "ready", "--limit", "50"])
    assert controller.main() == 0
    printed = json.loads(capsys.readouterr().out)
    listed = [int(row["target"].split(":")[1]) for row in printed["items"]]
    assert walked and listed == walked
    assert 2 not in walked and 4 not in walked and 6 not in walked
    assert printed["held"]["issue:4"] == "held: cooling"
    assert printed["held"]["issue:2"] == "blocked by open #1, #6"


def test_a_failed_ready_read_is_a_warning_not_a_crash() -> None:
    def broken() -> Any:
        raise RuntimeError("discovery failed")

    found = ready.report("o/r", broken, 10, True)
    assert found["items"] == [] and found["error"] == "RuntimeError"
