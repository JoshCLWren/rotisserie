"""``baste ready``, ``baste next`` and ``baste why`` read the dispatcher's own pool."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_factory_console import FakeGitHub, fake, invoke  # type: ignore[import-not-found]

from rotisserie import control_contract as contract
from rotisserie.control_parity import CONTROLS
from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import frontier, providers, queue
from rotisserie.factory_console.service import ready as ready_service

__all__ = ["fake"]

REPO = "acme/widgets"
NOW = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def issue(number: int, *labels: str, body: str = "") -> dict[str, Any]:
    return {
        "number": number,
        "state": "OPEN",
        "title": f"Issue {number}",
        "body": body,
        "labels": ["factory", *labels],
        "createdAt": f"2026-11-{number:02d}T00:00:00Z",
    }


ISSUES = [
    issue(1),
    issue(2, body="Blocked by #1"),
    issue(3, "priority:high"),
    issue(4, body="Depends on #5"),
    issue(5, body="Blocked by #4"),
    issue(6),
]


def capture(monkeypatch: pytest.MonkeyPatch) -> frontier.Frontier:
    board = queue.Board(ISSUES, [], 8, 6, None)
    ready = queue.Verdict("ready", ())
    entries = tuple(
        queue.QueueEntry(int(row["number"]), str(row["title"]), ready) for row in ISSUES
    )
    summary = queue.QueueSummary(entries, board)
    monkeypatch.setattr(queue, "summarize_queue", lambda *_a, **_k: summary)
    monkeypatch.setattr(providers, "repository_variables", lambda _gh: {})
    monkeypatch.setattr(frontier, "_active_leases", lambda _repo: ({}, ""))
    monkeypatch.setattr(frontier, "_retry_history", lambda *_a: [])
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    return frontier.capture(cast(GitHub, SimpleNamespace(repo=REPO)), now=NOW)


def test_ready_lists_the_pool_in_the_dispatchers_order(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = capture(monkeypatch).to_dict()
    data = cast(dict[str, Any], ready_service.project(snapshot))
    pool = runtime.module("factory_ready").pool(ISSUES, [], attempt_records=[])
    walked = [f"{c.kind}:{c.number}" for c in pool.candidates]
    assert [row["target"] for row in data["ready"]] == walked == ["issue:3", "issue:1", "issue:6"]
    held = {row["target"]: row["reasons"] for row in data["held"]}
    assert "blocked by open #1" in held["issue:2"]
    assert any(reason.startswith("in a dependency cycle") for reason in held["issue:4"])
    assert data["cycles"] == [[4, 5]]
    assert data["authoritative"] is False and ready_service.NO_STORE in data["notes"]
    assert [row["stage"] for row in data["ready"]] == ["new", "new", "new"]
    assert {row["action"] for row in data["ready"]} == {"implement"}


def test_next_marks_a_blocked_issue_held_with_its_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    found = capture(monkeypatch)
    offers = {offer.target: offer for offer in found.offers}
    holds = {str(hold["target"]): hold for hold in found.holds}
    assert offers["issue:1"].ready and "issue:2" not in offers
    assert holds["issue:2"]["status"] == "held"
    assert holds["issue:2"]["reasons"] == ["blocked by open #1"]
    assert found.cycles == ((4, 5),)


def test_baste_ready_prints_the_list_and_the_cycle(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = capture(monkeypatch)
    monkeypatch.setattr(frontier, "capture", lambda *_a, **_k: snapshot)
    code, out = invoke(["ready"])
    assert code == 0
    assert "Ready work (3)" in out and "issue:3" in out
    assert "Dependency cycle: #4 -> #5 -> #4" in out
    code, out = invoke(["ready", "--json"])
    data = json.loads(out)
    assert [row["rank"] for row in data["ready"]] == [1, 2, 3]


def test_why_names_the_links_and_their_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    found = ready_service.dependencies(REPO, ISSUES, 2)
    assert found["blocked_by"] == [1] and found["reason"] == "blocked by open #1"
    assert found["links"] == [{"src": 1, "dst": 2, "kind": "blocks", "origin": "body"}]
    assert found["cycles"] == [[4, 5]] and found["store_checked"] is False


def test_both_control_rooms_serve_ready() -> None:
    assert contract.read_command("ready", {}) == ["ready"]
    assert "ready" in contract.PUBLIC_READS and CONTROLS["ready"] == ("read:ready",)
    ui = (ROOT / "src/rotisserie/web_ui.js").read_text()
    assert 'get("ready")' in ui
    assert 'id="queue-ready"' in (ROOT / "src/rotisserie/web_ui.html").read_text()
    hosted = (ROOT / "factory/scripts/hosted/public_reads.js").read_text()
    assert 'name === "ready"' in hosted and "stats.ready" in hosted
