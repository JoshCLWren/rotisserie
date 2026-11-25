"""``baste turning`` and ``recap`` name an incident's looping or parked PR as incident work."""

from __future__ import annotations

from typing import Any, cast

import pytest
from test_factory_item_loops import (  # type: ignore[import-not-found]
    AT_0130,
    TITLE,
    Tracker,
    evidence,
    pull,
    recorded,
    runs,
    sources,
)

from rotisserie.factory_console.commands import turning as view
from rotisserie.factory_console.service import (
    doctor,
    failures,
    fleet,
    incident,
    loops,
    parked,
    recap,
    recap_build,
    reliability,
    review,
    self_heal,
    stalls,
    turning,
)
from rotisserie.factory_console.service.fleet import FleetState
from rotisserie.factory_console.ui import Console

INCIDENT = 5746
CHECKPOINT = f"factory/work-checkpoints/issue-{INCIDENT}/iceman/45578987720"


def incident_pull(number: int, title: str) -> dict[str, Any]:
    return {**pull(number, title), "head": {"sha": "d" * 40, "ref": CHECKPOINT}}


def incident_scan() -> list[dict[str, object]]:
    rows = loops.scan(
        None,  # type: ignore[arg-type]
        now=AT_0130,
        tracker=Tracker({row["run"]: row for row in recorded()}),
        runs=runs(),
        pulls=[incident_pull(5766, TITLE), pull(5674, "Cover sizes")],
        incident_issues={INCIDENT},
    )
    assert [row["target"] for row in rows] == ["pr:5766", "pr:5674"]
    return rows


def test_scan_marks_the_incident_pr() -> None:
    rows = {row["target"]: row for row in incident_scan()}
    assert rows["pr:5766"]["incident"] is True
    assert rows["pr:5766"]["next"] == loops.INCIDENT_NEXT
    assert rows["pr:5674"]["incident"] is False


def test_turning_names_incident_work_and_leads_with_the_real_loop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    verdict = turning.assess(evidence(loops=incident_scan()))
    assert verdict.text == (
        "Cover sizes is looping and 1 more: 4 of 4 worker runs in the last 6h changed nothing; "
        "most often work_failure (3)."
    )
    view.render(Console(), verdict.to_dict(), AT_0130)
    out = capsys.readouterr().out
    assert f"{TITLE} (#5766) [incident]: 7 of 8 worker runs" in out
    only = turning.assess(evidence(loops=incident_scan()[:1]))
    assert only.text == (
        f"{TITLE} is incident work, not held: 7 of 8 worker runs in the last 6h changed "
        "nothing; most often work_failure (5)."
    )


def test_recap_shows_incident_work_not_a_loop_or_a_park() -> None:
    held = [
        {"number": 5737, "title": "Queue covers", "url": "u", "text": "t", "incident": True},
    ]
    rows = recap_build.needs_you(sources(loops=incident_scan(), parked=held))
    found = {(row["kind"], str(row["text"]).split(":")[0]) for row in rows}
    assert ("incident", f"{TITLE} (#5766) is incident work") in found
    assert ("incident", "Queue covers (#5737) is incident work") in found
    assert ("loop", "Cover sizes (#5674) is looping") in found
    assert not any("#5766) is looping" in str(row["text"]) for row in rows)
    assert not any("#5737) is parked" in str(row["text"]) for row in rows)


def test_the_console_backlog_does_not_count_an_incident_pr_as_parked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(number: int, branch: str) -> dict[str, Any]:
        names = ("factory", "factory:unowned", "factory:review")
        return {
            "number": number,
            "state": "OPEN",
            "title": f"PR {number}",
            "body": f"Refs #{INCIDENT}.",
            "labels": [{"name": name} for name in names],
            "headRefName": branch,
            "headRefOid": "d" * 40,
        }

    prs = [row(5772, CHECKPOINT), row(5681, "factory/20-2407-queue-covers")]
    monkeypatch.setattr(loops, "parked", lambda *_a, **_k: [{"number": 5772}, {"number": 5681}])
    stamped = parked.stamp(None, prs, 1, 0, frozenset({5772}), frozenset({INCIDENT}))  # type: ignore[arg-type]
    marks = {r["number"]: r.get("factoryLoopHold") for r in stamped}
    assert marks == {5772: None, 5681: "held: loop-parked"}


def declare(monkeypatch: pytest.MonkeyPatch) -> None:
    active = incident.Incident(INCIDENT, 3, issue_state="open", labeled=True)
    ended = incident.Incident(41, 3, issue_state="closed", labeled=False)
    monkeypatch.setattr(incident, "read", lambda _g: [active, ended])


def test_turning_reads_mark_the_live_incident_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch)
    monkeypatch.setattr(failures, "runs_since", lambda *_a, **_k: runs())
    monkeypatch.setattr(
        review, "open_pulls", lambda _g: [incident_pull(5766, TITLE), pull(5674, "Cover sizes")]
    )
    tracker = Tracker({row["run"]: row for row in recorded()})
    rows = loops.live(None, now=AT_0130, tracker=tracker)  # type: ignore[arg-type]
    assert {row["number"]: row["incident"] for row in rows} == {5766: True, 5674: False}


def test_recap_reads_mark_the_live_incident_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch)
    for module, name, value in (
        (doctor, "run_checks", []),
        (failures, "runs_since", []),
        (failures, "worker_failures", []),
        (stalls, "scan", []),
        (self_heal, "recent", []),
        (reliability, "load_stats", None),
        (recap, "current_work", []),
        (recap, "search", []),
        (loops, "parked", []),
    ):
        monkeypatch.setattr(module, name, lambda *_a, _v=value, **_k: _v)
    monkeypatch.setattr(fleet, "read", lambda _g: FleetState({}, {}))
    monkeypatch.setattr(review, "open_pulls", lambda *_a: [incident_pull(5766, TITLE)])
    looping = {"target": "pr:5766", "number": 5766, "title": TITLE, "text": "t", "next": "n"}
    monkeypatch.setattr(loops, "scan", lambda *_a, **_k: [dict(looping)])

    class Hub:
        repo = "acme/widgets"

    data = recap.recap(Hub(), since=AT_0130, now=AT_0130).to_dict()  # type: ignore[arg-type]
    kinds = [(row["kind"], row["text"]) for row in cast(list[dict[str, object]], data["needs_you"])]
    assert kinds == [("incident", f"{TITLE} (#5766) is incident work: t")]
