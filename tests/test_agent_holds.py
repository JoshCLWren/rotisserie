"""Outside agent holds in the label doctor: left alone, then released after a quiet day."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from test_label_claims import NOW, FakeGitHub, offer, snapshot  # type: ignore[import-not-found]

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import agent_holds, frontier, labels


@pytest.fixture(autouse=True)
def no_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frontier, "_active_leases", lambda repo: ({}, ""))


def held(github: FakeGitHub, number: int, idle: timedelta) -> None:
    github.items[number] = {"factory", "factory:review", "factory:claimed", "agent:chatgpt"}
    github.kinds[number] = "pr"
    github.updated[number] = (NOW - idle).isoformat().replace("+00:00", "Z")


def test_the_doctor_leaves_an_agent_held_item_and_its_claim_alone() -> None:
    github = FakeGitHub()
    held(github, 7, timedelta(hours=2))
    report = labels.repair(
        cast(GitHub, github), now=NOW, snapshot=snapshot(offer("pr:7", "review"))
    )
    assert github.writes == []
    assert {"target": "pr:7", "holds": ["outside agent agent:chatgpt"]} in report.holds
    assert report.released_agents == []
    assert not github.events


def test_a_quiet_day_releases_the_agent_label_with_one_comment() -> None:
    github = FakeGitHub()
    held(github, 7, timedelta(hours=25))
    report = labels.repair(cast(GitHub, github), now=NOW, snapshot=snapshot())
    assert ("remove", 7, "agent:chatgpt") in github.writes
    assert report.released_agents == [
        {"number": 7, "agents": ["agent:chatgpt"], "status": "released"}
    ]
    comments: list[dict[str, Any]] = github.comments[7]
    assert len(comments) == 1 and "24 hours" in comments[0]["body"]


def test_only_the_scheduled_full_pass_releases() -> None:
    github = FakeGitHub()
    held(github, 7, timedelta(hours=25))
    rows = [github._row(7)]
    labels.repair(cast(GitHub, github), now=NOW, snapshot=snapshot(), items=rows)
    assert github.writes == []
    assert agent_holds.stale(rows, NOW)[0]["number"] == 7
