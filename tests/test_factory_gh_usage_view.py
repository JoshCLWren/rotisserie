"""`baste recap` and `baste limits` show requests per factory job and the factory budget."""

from __future__ import annotations

import argparse
import base64
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from rotisserie.factory_console import commands
from rotisserie.factory_console.commands.recap import render
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import gh_usage
from rotisserie.factory_console.service import recap_build as build
from rotisserie.factory_console.tracker import Clock
from rotisserie.factory_console.ui import Console

NOW = datetime(2026, 11, 25, 15, 10, tzinfo=UTC)
AT = int(NOW.timestamp())


def _job(at: int, github: int, workflow: int, remaining: int | None) -> dict[str, object]:
    rest = None if remaining is None else {"remaining": remaining, "limit": 5000, "reset": AT}
    return {
        "at": at,
        "label": "dispatch roster",
        "github": github,
        "cache_hits": 3,
        "writes": 2,
        "paginated": 5,
        "buckets": {"workflow": workflow, "reader": github - workflow},
        "top": [["api GET repos/R/pulls/N", 24], ["api GET repos/R/issues", 10]],
        "budget": rest,
    }


STATS: dict[str, Any] = {
    "gh_usage": {
        "generated_at": NOW.isoformat(),
        "jobs": {
            "dispatch": [_job(AT - 300, 100, 10, 1900), _job(AT - 60, 60, 6, 1806)],
            "merge": [_job(AT - 120, 122, 122, None)],
        },
    }
}


def test_published_usage_averages_each_kind_and_keeps_the_newest_budget() -> None:
    report = gh_usage.from_stats(STATS)
    assert report["status"] == "ok"
    dispatch, merge = report["kinds"]
    assert (dispatch["label"], dispatch["jobs"], dispatch["average"], dispatch["most"]) == (
        "dispatcher",
        2,
        80,
        100,
    )
    assert dispatch["average_workflow"] == 8
    assert dispatch["top"][0] == ("api GET repos/R/pulls/N", 48)
    assert merge["average_workflow"] == 122
    assert report["budget"] == {"remaining": 1806, "limit": 5000, "reset": AT, "at": AT - 60}


def test_a_budget_below_the_dispatch_reserve_warns() -> None:
    low: dict[str, Any] = {"gh_usage": {"jobs": {"dispatch": [_job(AT, 50, 5, 900)]}}}
    report = gh_usage.from_stats(low)
    assert report["status"] == "warn"
    assert "skips its pass below 1,200" in gh_usage.budget_line(report["budget"])


def test_nothing_published_reads_as_unpublished() -> None:
    assert gh_usage.from_stats({})["status"] == "unpublished"
    assert gh_usage.from_stats(None)["kinds"] == []


def test_recap_shows_a_github_section_once_published() -> None:
    sources = build.Sources(since=NOW, now=NOW, stats={})
    assert build.github_usage(sources) is None
    sources = build.Sources(since=NOW, now=NOW, stats=STATS)
    section = build.github_usage(sources)
    assert section is not None and section["status"] == "ok"
    lines = section["lines"]
    assert isinstance(lines, list)
    assert lines[0].startswith("dispatcher: about 80 GitHub requests per job")
    assert lines[-1] == "Factory workflow token budget: 1,806 of 5,000 left"
    stream = io.StringIO()
    render(Console(stream, env={"NO_COLOR": "1"}), _recap_data(section))
    assert "GitHub requests" in stream.getvalue()
    assert "1,806 of 5,000 left" in stream.getvalue()


def _recap_data(section: dict[str, object]) -> dict[str, object]:
    return {
        "since": NOW.isoformat(),
        "verdict": "Quiet",
        "merged": {},
        "needs_you": [],
        "healed": [],
        "clearing": [],
        "went_wrong": {},
        "providers": {},
        "stuck": [],
        "database": None,
        "unread": [],
        "github": section,
    }


def _limits(monkeypatch: pytest.MonkeyPatch, stats: object) -> tuple[str, list[str]]:
    calls: list[str] = []
    quota = {"resources": {"core": {"limit": 5000, "remaining": 4000, "used": 1000, "reset": AT}}}

    def github_run(self: GitHub, args: Sequence[str], **_: object) -> str:
        calls.append(args[1])
        if args[1] == "rate_limit":
            return json.dumps(quota)
        encoded = base64.b64encode(json.dumps(stats).encode()).decode()
        return json.dumps({"content": encoded})

    monkeypatch.setattr(GitHub, "_run", github_run)
    parser = argparse.ArgumentParser()
    commands.add_parser(parser.add_subparsers(dest="command"))
    stdout = io.StringIO()
    code = commands.main(
        parser.parse_args(["factory", "limits"]),
        env={"FACTORY_REPO": "acme/widgets"},
        stdout=stdout,
        clock=Clock(now=lambda: NOW),
    )
    assert code == 0
    return stdout.getvalue(), calls


def test_limits_adds_the_factory_budget_with_one_snapshot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, calls = _limits(monkeypatch, STATS)
    assert calls[0] == "rate_limit"
    assert len(calls) == 2 and calls[1].startswith("repos/acme/widgets/contents/factory-stats.json")
    assert "Factory workflow token budget: 1,806 of 5,000 left" in out
    assert "merge drain: about 122 GitHub requests per job" in out


def test_limits_says_when_nothing_is_published(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = _limits(monkeypatch, {})
    assert "No factory job has published its GitHub request counts yet." in out
