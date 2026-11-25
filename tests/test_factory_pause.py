"""`baste pause` and `baste resume`: a maintenance drain the owner can undo exactly."""

from __future__ import annotations

import argparse
import io
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from rotisserie.factory_console.commands.pause import cmd_pause, cmd_resume, show_pause
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import JSON, GitHub, GitHubError, GitHubNotFoundError
from rotisserie.factory_console.service import pause
from rotisserie.factory_console.tracker import Clock, Result
from rotisserie.factory_console.ui import Console, Prompt

REPO = "acme/widgets"
NOW = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)
OWNER = "HTTP/2.0 200 OK\r\nX-Oauth-Scopes: repo, workflow\r\n\r\n" + '{"login": "owner"}'


class FakeGitHub(GitHub):
    def __init__(self, states: dict[str, str], *, scopes: str = OWNER) -> None:
        super().__init__(REPO)
        self.states = dict(states)
        self.ids = {name: index for index, name in enumerate(sorted(states), 1)}
        self.scopes = scopes
        self.variable: str | None = None
        self.variable_readable = True
        self.log: list[str] = []

    def gh(self, *args: str, stdin: str | None = None, allow_exit_one: bool = False) -> str:
        if args == ("api", "-i", "user"):
            return self.scopes
        if args[:2] == ("variable", "set"):
            self.variable = args[args.index("--body") + 1]
            self.log.append("variable set")
            return ""
        if args[:2] == ("variable", "delete"):
            self.variable = None
            self.log.append("variable delete")
            return ""
        raise AssertionError(args)

    def repo_api(self, path: str, **kwargs: object) -> JSON:
        method = str(kwargs.get("method", "GET"))
        if path.startswith("actions/workflows?"):
            return {
                "workflows": [
                    {"id": self.ids[name], "path": f".github/workflows/{name}", "state": state}
                    for name, state in self.states.items()
                ]
            }
        if path == f"actions/variables/{pause.VARIABLE}":
            if not self.variable_readable:
                raise GitHubError("Resource not accessible by integration")
            if self.variable is None:
                raise GitHubNotFoundError("no variable")
            return {"name": pause.VARIABLE, "value": self.variable}
        if method == "PUT":
            workflow_id = int(path.split("/")[2])
            name = next(key for key, value in self.ids.items() if value == workflow_id)
            verb = path.rsplit("/", 1)[-1]
            self.states[name] = "active" if verb == "enable" else "disabled_manually"
            self.log.append(f"{verb} {name}")
            return None
        raise AssertionError(path)


def context(github: FakeGitHub) -> tuple[Context, io.StringIO]:
    stream = io.StringIO()
    console = Console(stream, env={})
    return Context(cast(GitHub, github), console, Prompt(console), Clock(now=lambda: NOW), False), (
        stream
    )


def installed(**overrides: str) -> dict[str, str]:
    states = {name: "active" for name in pause.WORKFLOWS}
    states["factory-checkpoint-reconcile.yml"] = "disabled_manually"
    return {**states, **overrides}


def test_pause_turns_off_only_active_factory_workflows_and_records_why() -> None:
    github = FakeGitHub(installed(**{"factory-status-pages.yml": "disabled_manually"}))
    ctx, stream = context(github)
    outcome = cmd_pause(ctx, argparse.Namespace(reason="store migration", yes=True))
    assert outcome.result == Result.COMPLETED
    assert github.log[0] == "variable set"
    assert {call for call in github.log[1:]} == {
        f"disable {name}" for name in pause.WORKFLOWS if name != "factory-status-pages.yml"
    }
    assert github.states["factory-checkpoint-reconcile.yml"] == "disabled_manually"
    recorded = json.loads(github.variable or "{}")
    assert recorded["reason"] == "store migration" and recorded["by"] == "owner"
    assert "factory-status-pages.yml" not in recorded["disabled"]
    text = stream.getvalue()
    assert "Turned off dispatcher" in text
    assert "factory-status-pages.yml was already off; resume leaves it off" in text


def test_resume_turns_on_exactly_what_pause_turned_off() -> None:
    github = FakeGitHub(installed(**{"factory-status-pages.yml": "disabled_manually"}))
    ctx, stream = context(github)
    cmd_pause(ctx, argparse.Namespace(reason="drain", yes=True))
    github.log.clear()
    outcome = cmd_resume(ctx, argparse.Namespace(yes=True))
    assert outcome.result == Result.COMPLETED
    assert github.states["factory-status-pages.yml"] == "disabled_manually"
    assert github.states["factory-checkpoint-reconcile.yml"] == "disabled_manually"
    assert github.states["fixed-model-factory-dispatch.yml"] == "active"
    assert github.log[-1] == "variable delete" and github.variable is None
    assert len(outcome.data["turned_on"]) == len(pause.WORKFLOWS) - 1  # type: ignore[arg-type]
    assert "Turned on label claim" in stream.getvalue()


def test_pause_without_the_owners_workflow_scope_fails_clearly() -> None:
    actions = "HTTP/2.0 200 OK\r\n\r\n" + '{"login": "github-actions[bot]"}'
    github = FakeGitHub(installed(), scopes=actions)
    ctx, _ = context(github)
    with pytest.raises(UsageError, match="workflow scope"):
        cmd_pause(ctx, argparse.Namespace(reason="x", yes=True))
    with pytest.raises(UsageError, match="gh auth refresh -s workflow"):
        cmd_resume(ctx, argparse.Namespace(yes=True))
    assert github.log == []


def test_status_shows_the_pause_reason_and_time() -> None:
    github = FakeGitHub(installed())
    github.variable = json.dumps({"since": "2026-11-25T15:00:00+00:00", "reason": "store work"})
    ctx, stream = context(github)
    data: dict[str, Any] = {}
    show_pause(ctx, data)
    assert data["paused"]["reason"] == "store work"
    assert "The factory is paused since " in stream.getvalue()
    assert ": store work" in stream.getvalue()


def test_a_token_that_cannot_read_variables_still_sees_the_pause() -> None:
    github = FakeGitHub(installed(**{"fixed-model-factory-dispatch.yml": "disabled_manually"}))
    github.variable_readable = False
    found = pause.state(cast(GitHub, github))
    assert found is not None and "dispatcher workflow is turned off" in str(found["reason"])
    github.states["fixed-model-factory-dispatch.yml"] = "active"
    assert pause.state(cast(GitHub, github)) is None


def test_no_pause_record_shows_nothing() -> None:
    github = FakeGitHub(installed())
    ctx, stream = context(github)
    data: dict[str, Any] = {}
    show_pause(ctx, data)
    assert data == {"paused": None} and stream.getvalue() == ""
