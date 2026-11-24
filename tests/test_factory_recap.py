from __future__ import annotations

import io
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, cast

import pytest

from rotisserie import control_contract
from rotisserie.factory_console import ui
from rotisserie.factory_console.commands.recap import render
from rotisserie.factory_console.github import GitHubError
from rotisserie.factory_console.service import (
    doctor,
    failures,
    fleet,
    recap,
    reliability,
    review,
    self_heal,
    stalls,
)
from rotisserie.factory_console.service import recap_build as build
from rotisserie.factory_console.service.doctor import Finding
from rotisserie.factory_console.service.failures import WorkerFailure
from rotisserie.factory_console.service.fleet import FleetState, Off
from rotisserie.factory_console.service.stalls import Stall
from rotisserie.factory_console.ui import Console
from rotisserie.factory_console.work import Item, WorkerRun

NOW = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=12)
AUTHORED = "Every available model authored this PR; the review stays queued for another model"


def failure(cls: str, provider: str = "Kilo", error: str = "") -> WorkerFailure:
    return WorkerFailure(
        run_id=1,
        url="https://github.com/acme/widgets/actions/runs/1",
        finished_at="2026-11-24T05:00:00Z",
        worker="7",
        job="run / factory",
        step="",
        error=error,
        outcome_class=cls,
        provider=provider,
        model="",
        target="",
    )


def pr(number: int, title: str, *labels: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "labels": [{"name": name} for name in labels],
        "pull_request": {"merged_at": "2026-11-24T06:00:00Z"},
    }


def full() -> build.Sources:
    return build.Sources(
        since=SINCE,
        now=NOW,
        merged=[
            pr(10, "feat(factory): live reads for the hosted control room"),
            pr(11, "Fix hidden Roll snoozes and restore Reset Dice (#5747)"),
            pr(12, "refactor(vercel): serve the API from one function (#5733)"),
        ],
        runs=[
            {"status": "completed", "conclusion": "success" if index == 0 else "failure"}
            for index in range(5)
        ],
        failures=[
            failure("control_plane_failure", "Kilo", AUTHORED),
            failure("work_failure", "NVIDIA", "worker failed: work_failure"),
            failure("provider_quota", "OpenRouter"),
        ],
        findings=[
            Finding("main verification", "ok", "main passed"),
            Finding("model availability", "fail", "0 of 2 workers succeeded", hint="see report"),
            Finding("dashboard publisher", "fail", "stale", fix="rerun"),
        ],
        pulls=[
            pr(20, "Reconcile plans list after a cross-tab rename", "factory", "factory:review"),
            pr(21, "Old gate held this", "factory", "factory:blocked"),
        ],
        stalls=[Stall(20, "answered-thread", "Thread answered, not resolved.", "Resolve it.", "")],
        claims=[
            {**pr(30, "Repair the importer", "factory:claimed"), "pull_request": None},
            {**pr(31, "Cleanup", "factory:claim-rejected"), "pull_request": None},
        ],
        stats={
            "providers": [
                {"source": "nvidia", "windows": {"24h": {"runs": 9, "delivered": 3}}},
                {"source": "kilo-auto", "windows": {"24h": {"runs": 5, "failed": 5}}},
            ],
            "models": [
                {
                    "source": "kilo-auto",
                    "model": "kilo/free",
                    "health": {
                        "status": "cooling",
                        "cooldown_until": "2026-11-24T20:00:00+00:00",
                        "last_outcome": "provider_failure",
                    },
                }
            ],
        },
        fleet=FleetState(
            providers={"orcarouter": Off("provider", "orcarouter", None, "out of credits", "")},
            models={},
        ),
        work=[
            Item(
                "issue",
                40,
                "Speed up the search page",
                set(),
                NOW,
                "https://github.com/acme/widgets/issues/40",
                owner="7",
                run=WorkerRun(5, "7", "in_progress", NOW - timedelta(hours=3), ""),
            ),
            Item(
                "issue",
                41,
                "Fresh work",
                set(),
                NOW,
                "",
                owner="7",
                run=WorkerRun(6, "7", "in_progress", NOW - timedelta(minutes=5), ""),
            ),
            Item("pr", 42, "Claimed and idle", set(), NOW - timedelta(hours=2), "", owner="8"),
        ],
    )


def test_titles_are_plain_and_grouped_product_or_factory() -> None:
    groups = build.merged(full())
    assert [row["title"] for row in groups["factory"]] == ["Live reads for the hosted control room"]
    assert [row["title"] for row in groups["product"]] == [
        "Fix hidden Roll snoozes and restore Reset Dice",
        "Serve the API from one function",
    ]
    assert build.is_factory("Authenticate Rotisserie dashboard administrators")
    assert build.is_factory("ci(factory): install the claim bridge")
    assert not build.is_factory("Option E: test named issue handoffs")
    assert build.plain_title("#4136 QA: prod browser") == "#4136 QA: prod browser"


def test_needs_you_lists_only_owner_actions() -> None:
    rows = build.needs_you(full())
    kinds = [row["kind"] for row in rows]
    assert kinds == ["doctor", "deadlock", "blocked", "claim", "claim", "credits"]
    texts = [str(row["text"]) for row in rows]
    assert texts[0] == "Model availability: 0 of 2 workers succeeded"
    assert texts[1].startswith("Reconcile plans list after a cross-tab rename (#20) is stuck")
    assert "Old gate held this (#21)" in texts[2]
    assert "Repair the importer (#30) has an open claim" in texts[3]
    assert rows[3]["action"] == "When you are done, run `baste finish issue:30`."
    assert texts[4] == "Your claim on Cleanup (#31) was refused."
    assert texts[5] == "OpenRouter ran out of quota or credits (1 failed runs)."
    assert not any("Dashboard" in text for text in texts)


def test_failing_main_needs_you_first() -> None:
    sources = full()
    sources.findings = [Finding("main verification", "fail", "lint failed", hint="fix main")]
    row = build.needs_you(sources)[0]
    assert row["kind"] == "main"
    assert row["text"] == "Main is failing: lint failed"


def test_failures_grouped_by_real_cause_with_fix() -> None:
    wrong = build.went_wrong(full())
    assert wrong["read"] == 3
    assert wrong["failed_runs"] == 4
    causes = {row["cause"]: row for row in cast(list[dict[str, object]], wrong["causes"])}
    assert causes["no_capacity"]["words"] == "no capacity"
    assert causes["no_capacity"]["example"] == AUTHORED
    assert str(causes["no_capacity"]["fix"]).startswith("Add a provider")
    assert causes["work_failure"]["fix"] is None
    assert causes["provider_quota"]["providers"] == ["OpenRouter"]


def test_providers_delivered_wasted_off_and_paused() -> None:
    found = build.providers(full())
    assert [row["delivered"] for row in found["delivered"]] == [3]
    assert [row["failed"] for row in found["wasted"]] == [5]
    assert found["off"][0]["reason"] == "out of credits"
    assert found["paused"][0]["until"] == "2026-11-24T20:00:00+00:00"
    assert found["paused"][0]["why"] == "provider error"


def test_stuck_lists_long_runs_and_idle_claims_only() -> None:
    rows = build.stuck(full())
    assert [row["ref"] for row in rows] == ["issue:40", "pr:42"]
    assert rows[0]["text"] == "Speed up the search page (#40) has had a worker on it for 3 hours."
    assert (
        rows[1]["text"] == "Claimed and idle (#42) is claimed, but no worker has run for 2 hours."
    )


VERDICT = {
    "merged_count": 6,
    "main": "green",
    "scheduler_stopped": False,
    "capacity": 0,
    "failures_read": 0,
    "failed_runs": 4,
    "finished": 10,
    "succeeded": 6,
    "needs": 0,
}


@pytest.mark.parametrize(
    ("change", "tone", "text"),
    [
        ({}, "good", "Good night: 6 PRs merged, main green"),
        ({"merged_count": 1}, "good", "Good night: 1 PR merged, main green"),
        ({"main": "failing"}, "rough", "Rough night: main is failing, 6 PRs merged"),
        ({"scheduler_stopped": True}, "rough", "Rough night: the scheduler stopped, 6 PRs merged"),
        (
            {"capacity": 5, "failures_read": 8, "failed_runs": 8},
            "rough",
            "Rough night: most failed runs had no capacity, 6 PRs merged",
        ),
        (
            {"merged_count": 0, "succeeded": 0},
            "rough",
            "Rough night: nothing merged and no run succeeded",
        ),
        ({"merged_count": 0}, "quiet", "Quiet night: nothing merged, main green"),
        (
            {"capacity": 2, "failures_read": 3, "failed_runs": 3, "finished": 200},
            "good",
            "Good night: 6 PRs merged, main green",
        ),
        ({"main": "unverified"}, "good", "Good night: 6 PRs merged, main not verified yet"),
        ({"needs": 1}, "good", "Good night: 6 PRs merged, main green. 1 thing needs you"),
        ({"needs": 3}, "good", "Good night: 6 PRs merged, main green. 3 things need you"),
    ],
)
def test_verdict_wording(change: dict[str, Any], tone: str, text: str) -> None:
    assert build.verdict(**{**VERDICT, **change}) == (tone, text)


def test_database_shows_published_turso_usage_only() -> None:
    sources = full()
    assert build.database(sources) is None
    assert sources.stats is not None
    sources.stats["db_usage"] = {"status": "not_configured"}
    assert build.database(sources) is None
    sources.stats["db_usage"] = {
        "status": "warn",
        "metrics": {"rows_read": {"organization": 400e6, "allowance": 500e6, "share_used": 0.8}},
    }
    assert build.database(sources) == {
        "status": "warn",
        "text": "rows read 400.0M of 500.0M (80.0%)",
    }
    assert "! rows read 400.0M" in rendered(recap.assemble(sources).to_dict())


def test_assemble_full_recap() -> None:
    data = recap.assemble(full()).to_dict()
    assert data["verdict"] == "Good night: 3 PRs merged, main green. 6 things need you"
    assert data["main"] == "green"
    assert data["database"] is None


@pytest.fixture
def chicago(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Frozen:
            return cls.fromtimestamp(NOW.timestamp(), tz)

    monkeypatch.setattr(ui, "datetime", Frozen)
    yield
    monkeypatch.undo()
    time.tzset()


def rendered(data: dict[str, object]) -> str:
    stream = io.StringIO()
    render(Console(stream, env={"NO_COLOR": "1"}), data)
    return stream.getvalue()


def test_render_shows_sections_in_order_with_local_times(chicago: None) -> None:
    text = rendered(recap.assemble(full()).to_dict())
    assert "Morning recap since Mon 7:00 PM" in text
    order = [
        "Good night",
        "Merged: product",
        "Merged: factory",
        "Needs you",
        "What went wrong",
        "Providers",
        "Stuck",
    ]
    positions = [text.index(word) for word in order]
    assert positions == sorted(positions)
    assert "Serve the API from one function  #12" in text
    assert "until Tue 2:00 PM" in text
    assert "from the newest 3 of 4 failed runs" in text
    assert "Database" not in text


def test_render_omits_empty_sections(chicago: None) -> None:
    text = rendered(recap.assemble(build.Sources(since=SINCE, now=NOW)).to_dict())
    assert "Quiet night: nothing merged, main state unknown" in text
    for heading in ("Merged", "Needs you", "What went wrong", "Providers", "Stuck", "Database"):
        assert heading not in text


def test_a_failed_read_is_named_and_the_rest_still_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: object, **__: object) -> object:
        raise GitHubError("rate limited")

    monkeypatch.setattr(doctor, "run_checks", broken)
    monkeypatch.setattr(failures, "runs_since", lambda *_a, **_k: [])
    monkeypatch.setattr(failures, "worker_failures", lambda *_a, **_k: [])
    monkeypatch.setattr(review, "open_pulls", lambda *_a: [])
    monkeypatch.setattr(stalls, "scan", lambda *_a: [])
    monkeypatch.setattr(self_heal, "recent", lambda *_a, **_k: [])
    monkeypatch.setattr(
        recap, "search", lambda _g, query: [pr(1, "Add search")] if "merged" in query else []
    )
    monkeypatch.setattr(reliability, "load_stats", lambda _g: None)
    monkeypatch.setattr(fleet, "read", lambda _g: FleetState({}, {}))
    monkeypatch.setattr(recap, "current_work", lambda *_a, **_k: [])

    class Hub:
        repo = "acme/widgets"

    data = recap.recap(Hub(), since=SINCE, now=NOW).to_dict()  # type: ignore[arg-type]
    assert data["unread"] == ["doctor"]
    assert data["verdict"] == "Good night: 1 PR merged, main state unknown"


def test_contract_allows_recap_with_a_bounded_window() -> None:
    assert "recap" in control_contract.LIVE_READS
    assert control_contract.read_command("recap", {}) == ["recap", "--since", "12h"]
    assert control_contract.read_command("recap", {"since": "2d"}) == ["recap", "--since", "2d"]
    with pytest.raises(control_contract.InvalidRequest):
        control_contract.read_command("recap", {"since": "forever"})
