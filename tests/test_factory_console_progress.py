"""`baste --progress`: one JSON line per step on stderr, so long commands show their work."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from test_factory_console import (  # type: ignore[import-not-found]
    FULL_BACKLOG,
    REPO,
    TTY,
    FakeClock,
    FakeGitHub,
    fake,
    live_catalog,
    unstick_routes,
)

from rotisserie import cli
from rotisserie.factory_console import commands
from rotisserie.factory_console.github import GitHub, GitHubError
from rotisserie.factory_console.service import dispatch
from rotisserie.factory_console.tracker import Clock

__all__ = ["fake", "live_catalog"]


def run(argv: list[str]) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    arguments = cli.parser().parse_args(["factory", "--repo", REPO, "--json", "--progress", *argv])
    out, err = TTY(), io.StringIO()
    code = commands.main(
        arguments,
        env={"TERM": "xterm"},
        stdout=out,
        stdin=TTY(),
        stderr=err,
        clock=FakeClock(),
        executable="sh",
    )
    return (
        code,
        json.loads(out.getvalue()),
        [json.loads(line) for line in err.getvalue().splitlines()],
    )


def test_unstick_reports_each_step_then_each_action_with_its_run(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    code, data, steps = run(["unstick", "--max", "2", "--yes"])
    assert code == 0
    phases = [step.get("phase") for step in steps]
    texts = [step["step"] for step in steps]
    assert texts[0] == "Reading open issues, pull requests, claims and settings"
    assert texts[1].startswith("Read ") and "wait on review or checks" in texts[1]
    first = {phase: phases.index(phase) for phase in dict.fromkeys(phases)}
    assert first["read"] < first["obsolete"] < first["checks"] < first["baseline"]
    assert first["baseline"] < first["models"] < first["plan"] < first["act"]
    checked = [step for step in steps if step.get("phase") == "checks"]
    assert [(step["done"], step["total"]) for step in checked] == [(i, 7) for i in range(8)]
    plan = next(step for step in steps if step.get("phase") == "plan")
    assert plan["assignments"] == 2 and plan["held"] == 1
    assert plan["step"].startswith("Plan: 2 workers to start")
    actions = [
        (step["phase"], step.get("target")) for step in steps if step["phase"] in ("act", "result")
    ]
    assert actions == [
        ("act", "pr:301"),
        ("result", "pr:301"),
        ("act", "pr:306"),
        ("result", "pr:306"),
    ]
    results = [step for step in steps if step["phase"] == "result"]
    assert all(step["url"].endswith("/actions/runs/900") for step in results)
    assert "(1 of 2)" in texts[phases.index("act")]
    assert [item["target"] for item in data["started"]] == ["pr:301", "pr:306"]
    assert data["failed"] == []


def test_a_failed_request_is_reported_and_the_rest_still_start(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    original = dispatch.submit

    def flaky(github: GitHub, clock: Clock, request: dispatch.Request) -> dispatch.Submission:
        if request.target == "pr:301":
            raise GitHubError("dispatch refused")
        return original(github, clock, request)

    monkeypatch.setattr(dispatch, "submit", flaky)
    code, data, steps = run(["unstick", "--max", "2", "--yes"])
    assert code == 0
    assert data["failed"] == [{"target": "pr:301", "error": "dispatch refused"}]
    assert [item["target"] for item in data["started"]] == ["pr:306"]
    errors = [step for step in steps if step.get("error")]
    assert [step["target"] for step in errors] == ["pr:301"]


def test_without_progress_nothing_is_written_to_stderr(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    arguments = cli.parser().parse_args(
        ["factory", "--repo", REPO, "--json", "unstick", "--dry-run"]
    )
    err = io.StringIO()
    code = commands.main(
        arguments, env={}, stdout=TTY(), stdin=TTY(), stderr=err, clock=FakeClock(), executable="sh"
    )
    assert code == 0
    assert err.getvalue() == ""
