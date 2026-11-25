"""`baste assign`, `baste assigned` and the agent label `baste finish` removes."""

from __future__ import annotations

import argparse
import io
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from rotisserie import control_contract as contract
from rotisserie.factory_console.commands.assign import cmd_assign, cmd_assigned
from rotisserie.factory_console.commands.claims import cmd_finish
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import JSON, GitHub, GitHubError, GitHubNotFoundError
from rotisserie.factory_console.service import assign, assign_task
from rotisserie.factory_console.tracker import Clock, Result
from rotisserie.factory_console.ui import Console, Prompt

REPO = "acme/widgets"
NOW = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)


class FakeGitHub(GitHub):
    def __init__(self, *, labels: set[str], known: set[str] | None = None) -> None:
        super().__init__(REPO)
        self.labels = set(labels)
        self.known = set(known or ())
        self.comments: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str, dict[str, str]]] = []
        self.stage = "factory:review"

    def gh(self, *args: str, stdin: str | None = None, allow_exit_one: bool = False) -> str:
        if args[:2] == ("api", "graphql") and "comments(last:100)" in args[3]:
            nodes = [{"body": row["body"], "url": row["html_url"]} for row in self.comments]
            item = {"comments": {"nodes": nodes}}
            return json.dumps({"data": {"repository": {"issueOrPullRequest": item}}})
        if args[:2] == ("api", "graphql"):
            nodes = [
                {"createdAt": "2026-11-25T15:16:00Z", "label": {"name": "agent:claude"}},
                {"createdAt": "2026-11-25T16:00:00Z", "label": {"name": "factory:review"}},
            ]
            pr = {
                "number": 7,
                "title": "Fix roll faces",
                "url": "u7",
                "timelineItems": {"nodes": nodes},
            }
            labels = [
                {"name": "agent:claude", "issues": {"nodes": []}, "pullRequests": {"nodes": [pr]}},
                {"name": "agent:freebuff", "issues": {"nodes": []}, "pullRequests": {"nodes": []}},
                {"name": "via:agent:x", "issues": {"nodes": [pr]}, "pullRequests": {"nodes": []}},
            ]
            return json.dumps({"data": {"repository": {"labels": {"nodes": labels}}}})
        if "--method" in args and args[args.index("--method") + 1] == "POST":
            self.labels.add(args[-1].split("=", 1)[1])
            self.writes.append(("POST", args[1], {}))
            return ""
        if "--method" in args and args[args.index("--method") + 1] == "DELETE":
            name = args[1].rsplit("/", 1)[-1].replace("%3A", ":")
            if name not in self.labels:
                raise GitHubNotFoundError("missing")
            self.labels.discard(name)
            self.writes.append(("DELETE", args[1], {}))
            return ""
        raise AssertionError(args)

    def repo_api(self, path: str, **kwargs: object) -> JSON:
        method = str(kwargs.get("method", "GET"))
        fields = cast(dict[str, str], kwargs.get("fields") or {})
        if method != "GET":
            self.writes.append((method, path, fields))
        if method == "DELETE":
            name = path.rsplit("/", 1)[-1].replace("%3A", ":")
            if name not in self.labels:
                raise GitHubNotFoundError("missing")
            self.labels.discard(name)
            return None
        if path == "labels" and method == "POST":
            self.known.add(fields["name"])
            return {}
        if path.startswith("labels/"):
            if path.removeprefix("labels/").replace("%3A", ":") not in self.known:
                raise GitHubNotFoundError("no label")
            return {}
        if path == "issues/7/labels":
            self.labels.add(fields["labels[]"])
            return []
        if path == "issues/7/comments" and method == "POST":
            self.comments.append({"body": fields["body"], "html_url": "https://x/c/1"})
            return {"html_url": "https://x/c/1"}
        if path.startswith("issues/7/comments"):
            return cast(JSON, self.comments)
        if path == "pulls/7":
            return {
                "number": 7, "state": "open", "title": "Fix roll faces", "mergeable_state": "clean",
                "body": "Closes #5", "head": {"ref": "factory/42-5-roll-faces", "sha": "a" * 40},
                "labels": [{"name": name} for name in sorted(self.labels | {self.stage})],
                "user": {"login": "factory-bot"},
            }  # fmt: skip
        if path == "issues/5":
            return {"number": 5, "state": "open", "body": "## Acceptance\n\n- Faces stay visible"}
        if path == "issues/7":
            return {"number": 7, "labels": [{"name": name} for name in sorted(self.labels)]}
        raise AssertionError(path)


def context(github: FakeGitHub) -> tuple[Context, io.StringIO]:
    stream = io.StringIO()
    console = Console(stream, env={})
    return Context(
        cast(GitHub, github), console, Prompt(console), Clock(now=lambda: NOW), False
    ), stream


def arguments(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "what": "pr:7", "agent": "Claude", "task": "", "task_file": "", "kind": None,
        "no_wait": True, "timeout": 600,
    }  # fmt: skip
    return argparse.Namespace(**{**values, **overrides})


@pytest.fixture(autouse=True)
def not_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    from rotisserie.factory_console.service import pause

    monkeypatch.setattr(pause, "record", lambda github: None)


def test_assign_claims_labels_and_posts_the_stage_task() -> None:
    github = FakeGitHub(labels={"agent:freebuff"}, known=set())
    ctx, stream = context(github)
    outcome = cmd_assign(ctx, arguments())
    assert outcome.result == Result.COMPLETED
    assert {"factory:claimed", "agent:claude"} <= github.labels
    assert "agent:freebuff" not in github.labels
    assert ("POST", "labels", {"name": "agent:claude", "color": assign.COLOR,
                               "description": assign.DESCRIPTION}) in github.writes  # fmt: skip
    body = github.comments[0]["body"]
    assert "**Task: review this PR.**" in body
    assert "Read issue #5" in body and "Faces stay visible" in body
    assert "`Model: <your exact model id>`" in body
    assert "baste finish pr:7" in body and "agent:claude" in body
    assert "comment `DONE` here and remove the `agent:claude` label" in body
    text = stream.getvalue()
    assert "pr:7 assigned to agent:claude" in text and "Removed agent:freebuff" in text
    assert outcome.data["kind"] == "review"


def test_assign_twice_does_not_post_the_same_task_again() -> None:
    github = FakeGitHub(labels=set(), known={"agent:claude"})
    ctx, _ = context(github)
    cmd_assign(ctx, arguments(task="Review the head."))
    second = cmd_assign(ctx, arguments(task="Review the head."))
    assert len(github.comments) == 1
    assert second.data["comment"] == "already posted"
    assert second.data["kind"] == "custom"


def test_a_refused_claim_labels_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from rotisserie.factory_console.service import claimops

    monkeypatch.setattr(
        claimops, "claim", lambda *a, **k: {"status": "conflict", "reason": "held by worker 4"}
    )
    github = FakeGitHub(labels=set())
    ctx, stream = context(github)
    outcome = cmd_assign(ctx, arguments(no_wait=False))
    assert outcome.result == Result.NO_WORK
    assert github.writes == [] and github.comments == []
    assert "held by worker 4" in stream.getvalue()


def test_assign_refuses_while_the_factory_is_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    from rotisserie.factory_console.service import pause

    monkeypatch.setattr(pause, "record", lambda github: {"reason": "migrating the store"})
    github = FakeGitHub(labels=set())
    ctx, stream = context(github)
    assert cmd_assign(ctx, arguments()).result == Result.NO_WORK
    assert github.writes == []
    assert "paused: migrating the store" in stream.getvalue()


def test_bad_agent_names_and_two_task_sources_are_usage_errors() -> None:
    ctx, _ = context(FakeGitHub(labels=set()))
    with pytest.raises(UsageError):
        cmd_assign(ctx, arguments(agent="has space"))
    with pytest.raises(UsageError):
        cmd_assign(ctx, arguments(task="a", task_file="b"))


def test_finish_also_removes_the_agent_label(monkeypatch: pytest.MonkeyPatch) -> None:
    from rotisserie.factory_console.service import claimops

    monkeypatch.setattr(
        claimops, "finish", lambda *a, **k: {"status": "not-claimed", "target": "pr:7"}
    )
    github = FakeGitHub(labels={"agent:claude", "factory:review"})
    ctx, stream = context(github)
    outcome = cmd_finish(ctx, argparse.Namespace(what="pr:7", no_wait=True, timeout=600))
    assert outcome.data["agent_labels_removed"] == ["agent:claude"]
    assert github.labels == {"factory:review"}
    assert "Removed agent:claude" in stream.getvalue()


def test_assigned_lists_who_holds_what_and_for_how_long() -> None:
    ctx, stream = context(FakeGitHub(labels={"agent:claude"}))
    outcome = cmd_assigned(ctx, argparse.Namespace())
    assert outcome.data["assigned"] == [
        {
            "target": "pr:7", "title": "Fix roll faces", "agent": "claude",
            "since": "2026-11-25T15:16:00Z", "url": "u7", "held_for": "2h 44m",
        }
    ]  # fmt: skip
    assert "Held by outside agents (1)" in stream.getvalue()


def test_each_stage_has_a_default_task() -> None:
    assert set(assign_task.STEPS) == set(assign_task.TITLES) == set(assign_task.KINDS)
    for kind, steps in assign_task.STEPS.items():
        assert any("Model:" in step for step in steps), kind


def test_the_control_rooms_send_assign_but_never_pause_or_resume() -> None:
    assert contract.write_command(
        {"operation": "assign", "target": "pr:7", "agent": "claude", "kind": "ci-repair"}
    ) == ["assign", "pr:7", "--agent", "claude", "--no-wait", "--kind=ci-repair"]
    for owner_only in ("pause", "resume"):
        assert owner_only not in contract.WRITE_OPERATIONS
    assert contract.read_command("assigned", {}) == ["assigned"]
    for bad in (
        {"operation": "assign", "target": "pr:7", "agent": "Bad Name"},
        {"operation": "assign", "target": "pr:7", "agent": "x", "kind": "advance"},
    ):
        with pytest.raises(contract.InvalidRequest):
            contract.write_command(bad)


def test_a_pr_stage_without_outside_work_needs_a_named_task() -> None:
    github = FakeGitHub(labels=set())
    github.stage = "factory:ready"
    ctx, _ = context(github)
    with pytest.raises(UsageError, match="factory:ready, which has no default task"):
        cmd_assign(ctx, arguments())
    assert github.writes == []


def test_finish_still_reports_when_the_agent_label_cannot_be_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rotisserie.factory_console.service import claimops

    monkeypatch.setattr(
        claimops, "finish", lambda *a, **k: {"status": "released", "target": "pr:7"}
    )

    def broken(*args: object, **kwargs: object) -> list[str]:
        raise GitHubError("GitHub request failed: HTTP 502")

    monkeypatch.setattr(assign, "remove_agent_labels", broken)
    ctx, stream = context(FakeGitHub(labels={"agent:claude"}))
    outcome = cmd_finish(ctx, argparse.Namespace(what="pr:7", no_wait=True, timeout=600))
    assert outcome.result == Result.COMPLETED
    assert "HTTP 502" in str(outcome.data["agent_labels_error"])
    assert "Could not remove the agent:* labels" in stream.getvalue()


def test_status_turning_and_recap_show_the_pause_first() -> None:
    import inspect

    from rotisserie.factory_console.commands import recap, status, turning

    for command in (status.cmd_status, turning.cmd_turning, recap.cmd_recap):
        assert "show_pause(context" in inspect.getsource(command), command.__name__
