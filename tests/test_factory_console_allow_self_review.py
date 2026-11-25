"""`baste allow-self-review` and how `baste review` shows the owner's exception."""

from __future__ import annotations

import argparse
import io
from typing import cast

from rotisserie import control_contract as contract
from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands.self_review import cmd_allow_self_review
from rotisserie.factory_console.context import Context
from rotisserie.factory_console.github import JSON, GitHub, GitHubError, Obj
from rotisserie.factory_console.service import review
from rotisserie.factory_console.tracker import Clock, Result
from rotisserie.factory_console.ui import Console, Prompt

REPO = "acme/widgets"
HEAD, OLD = "a" * 40, "b" * 40
KIMI = "nvidia/moonshotai/kimi-k3.1-pro"
policy = runtime.module("factory_review_policy")
LABEL = runtime.module("factory_self_review_allowance").LABEL


def bot(body: str) -> Obj:
    return {"user": {"login": "github-actions[bot]"}, "author_association": "NONE", "body": body}


class FakeGitHub(GitHub):
    def __init__(self, *, user: Obj | None, labelled: bool) -> None:
        super().__init__(REPO)
        self.user = user
        self.labelled = labelled
        self.writes: list[tuple[str, str]] = []

    def api(self, path: str, **_kw: object) -> JSON:
        if self.user is None:
            raise GitHubError("Resource not accessible by integration")
        return self.user

    def repo_api(self, path: str, **kwargs: object) -> JSON:
        method = str(kwargs.get("method", "GET"))
        if method != "GET":
            self.writes.append((method, path))
            self.labelled = True
            return None
        labels = [{"name": "factory"}] + ([{"name": LABEL}] if self.labelled else [])
        rows: dict[str, JSON] = {
            "pulls/7": {
                "number": 7, "state": "open", "head": {"sha": HEAD, "ref": "factory/42-3-x"},
                "base": {"ref": "main"}, "labels": labels,
            },
            "issues/7/comments": [
                bot(policy.agent_model_author_marker(pr=7, head=HEAD, model=KIMI, agent="42"))
            ],
            "issues/7/events": [
                {"id": 5, "event": "labeled", "label": {"name": LABEL},
                 "actor": {"login": "JoshCLWren", "type": "User"},
                 "created_at": "2026-11-25T15:00:00Z"}
            ] if self.labelled else [],
            f"commits/{HEAD}": {
                "sha": HEAD, "parents": [{"sha": OLD}],
                "commit": {"message": "x", "committer": {"date": "2026-11-25T14:00:00Z"}},
            },
        }  # fmt: skip
        return next((value for key, value in rows.items() if path.startswith(key)), {})


def run(github: FakeGitHub) -> tuple[Result, str]:
    stream = io.StringIO()
    console = Console(stream, env={})
    context = Context(cast(GitHub, github), console, Prompt(console), Clock(), False)
    outcome = cmd_allow_self_review(context, argparse.Namespace(what="pr:7", yes=True))
    return outcome.result, stream.getvalue()


def test_a_person_adds_the_label_and_review_shows_the_exception() -> None:
    github = FakeGitHub(user={"login": "JoshCLWren", "type": "User"}, labelled=False)
    result, text = run(github)
    assert result == Result.COMPLETED
    assert github.writes == [("POST", "issues/7/labels")]
    assert "self-review allowed by owner (label by JoshCLWren)" in text
    report = review.inspect(github, 7)
    assert report.self_review_allowed_by == "JoshCLWren"
    lines = review.authorship_lines(report)
    assert "self-review allowed by owner (label by JoshCLWren)" in lines
    assert not any(line.startswith("Excluded from reviewing") for line in lines)


def test_an_actions_token_or_a_bot_cannot_grant_it() -> None:
    bots: tuple[Obj | None, ...] = (None, {"login": "factory-app[bot]", "type": "Bot"})
    for user in bots:
        github = FakeGitHub(user=user, labelled=False)
        result, text = run(github)
        assert result == Result.FAILED
        assert github.writes == []
        assert "Only a person may allow self-review" in text


def test_an_unlabelled_pr_still_excludes_the_head_author() -> None:
    report = review.inspect(FakeGitHub(user=None, labelled=False), 7)
    assert report.self_review_allowed_by == ""
    assert report.head_authors == ("nvidia+moonshotai+kimi-k3.1-pro",)


def test_the_control_rooms_run_the_cli_command() -> None:
    body = {"operation": "allow_self_review", "target": "pr:7"}
    assert contract.write_command(body) == ["allow-self-review", "pr:7", "--yes"]
