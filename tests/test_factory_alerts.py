"""baste alerts: the health signals that would have caught the Nov 26 factory breaks."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rotisserie.factory_console.github import Obj
from rotisserie.factory_console.service import alerts, failures, turning
from rotisserie.factory_console.tracker import Clock

FIXTURE = Path(__file__).parent / "fixtures" / "alerts_review_crash_2026_11_26.json"
NOW = datetime(2026, 11, 27, 4, 30, tzinfo=UTC)


def crash_runs() -> list[failures.WorkerFailure]:
    return [failures.WorkerFailure(**row) for row in json.loads(FIXTURE.read_text())["runs"]]


def test_the_review_verdict_crash_fires_a_repeated_failure_alert() -> None:
    """On Nov 26, 14 review runs in 20 minutes failed in the trusted review controller."""
    found = alerts.repeated_failures(crash_runs())
    assert len(found) == 1
    assert found[0].kind == "repeated-failure"
    assert found[0].text.startswith(
        "14 worker runs failed the same way in 20m (14 control_plane_failure)"
    )
    assert 'TRUSTED_REVIEW_CONTROLLER" review' in found[0].text


def test_scattered_or_unannotated_failures_raise_nothing() -> None:
    rows = crash_runs()
    assert alerts.repeated_failures(rows[:2]) == []
    blank = [replace(row, error="") for row in rows]
    assert alerts.repeated_failures(blank) == []


def test_only_a_slowing_or_burning_factory_raises_a_state_alert() -> None:
    assert alerts.state_alert({"verdict": turning.TURNING, "text": "ok"}) is None
    found = alerts.state_alert({"verdict": turning.BURNING, "text": "API rate limit exceeded"})
    assert found and found.text == "factory is burning: API rate limit exceeded"


def pull(number: int, *labels: str) -> dict[str, Any]:
    return {"number": number, "labels": [{"name": name} for name in ("factory", *labels)]}


def test_verdict_and_merge_droughts_need_waiting_prs() -> None:
    waiting = alerts.waiting_prs([pull(1, "factory:review"), pull(2, "factory:ready"), pull(3)])
    assert waiting == [1, 2]
    verdict: Obj = {"body": "<!-- x-factory-semantic-review-v1:pr-1:head-a:verdict-approve -->"}
    assert alerts.verdict_alert(waiting, [verdict]) is None
    assert alerts.verdict_alert([], []) is None
    plain: Obj = {"body": "plain"}
    drought = alerts.verdict_alert(waiting, [plain])
    assert drought and drought.kind == "no-verdict"
    fresh: Obj = {"merged_at": (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    stale: Obj = {"merged_at": (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert alerts.merge_alert(waiting, [fresh], NOW) is None
    unmerged: Obj = {"merged_at": None}
    stalled = alerts.merge_alert(waiting, [stale, unmerged], NOW)
    assert stalled and stalled.kind == "no-merge"
    assert alerts.merge_alert([], [stale], NOW) is None


class Hub:
    repo = "o/r"

    def __init__(self) -> None:
        self.paths: list[str] = []

    def repo_api(self, path: str, **_: object) -> object:
        self.paths.append(path)
        if path.startswith("pulls?state=open"):
            return [pull(5888, "factory:review")]
        return []


def test_one_pass_costs_three_reads_beyond_failures_and_turning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(failures, "worker_failures", lambda *_a, **_k: crash_runs())
    monkeypatch.setattr(turning, "gather", lambda *_a, **_k: object())
    monkeypatch.setattr(
        turning,
        "assess",
        lambda _found: type("V", (), {"to_dict": lambda self: {"verdict": "turning"}})(),
    )
    hub = Hub()
    found = alerts.check(hub, clock=Clock(now=lambda: NOW))  # type: ignore[arg-type]
    assert [alert.kind for alert in found] == ["repeated-failure", "no-verdict", "no-merge"]
    assert len(hub.paths) == 3


def test_follow_reports_only_starts_changes_and_clears() -> None:
    first = alerts.Alert("verdicts", "no-verdict", "no review verdict in 45m while 9 PRs wait")
    changed = alerts.Alert("verdicts", "no-verdict", "no review verdict in 45m while 3 PRs wait")
    assert alerts.changes({}, [first]) == [("alert", first)]
    assert alerts.changes({"verdicts": first}, [first]) == []
    assert alerts.changes({"verdicts": first}, [changed]) == []
    assert alerts.changes({"verdicts": changed}, []) == [("cleared", changed)]


def test_alerts_is_a_read_only_control_room_command() -> None:
    from rotisserie import control_contract, control_parity

    assert "alerts" in control_contract.READ_ONLY
    assert control_contract.read_command("alerts", {}) == ["alerts"]
    assert control_parity.CONTROLS["alerts"] == ("read:alerts",)


def test_loop_only_slowing_is_quiet_but_other_states_alert() -> None:
    """Overnight Nov 27, every looping V2 API PR raised Slowing; the loop breaker handles those."""
    assert alerts.state_alert({"verdict": "slowing", "reason": "item-loops", "text": "x"}) is None
    slowing = alerts.state_alert({"verdict": "slowing", "reason": "control-failures", "text": "y"})
    burning = alerts.state_alert({"verdict": "burning", "reason": "crash", "text": "z"})
    assert slowing and slowing.key == "state:slowing"
    assert burning and burning.key == "state:burning"
    assert alerts.VERDICT_WINDOW.total_seconds() == 90 * 60
    assert alerts.MERGE_WINDOW.total_seconds() == 150 * 60
