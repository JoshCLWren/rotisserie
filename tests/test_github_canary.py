"""Fail-closed tests for the manually dispatched self-dogfood merge canary."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from rotisserie.adapters.github.canary import CanaryMergeError, CanaryScope, merge_canary

REPOSITORY = "JoshCLWren/rotisserie"
HEAD = "a" * 40
SCOPE = CanaryScope(REPOSITORY, 9, "rotisserie/canary-9")


def pull(*, head: str = HEAD, fork: bool = False) -> str:
    return json.dumps(
        {
            "state": "open",
            "draft": False,
            "body": "Closes #9",
            "user": {"login": "producer"},
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
            "head": {
                "ref": SCOPE.branch,
                "sha": head,
                "repo": {"full_name": "someone/fork" if fork else REPOSITORY},
            },
        }
    )


@dataclass
class Runner:
    pulls: list[str] = field(default_factory=lambda: [pull(), pull()])
    reviews: str = field(
        default_factory=lambda: json.dumps(
            [{"state": "APPROVED", "commit_id": HEAD, "user": {"login": "reviewer"}}]
        )
    )
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, command: Sequence[str]) -> str:
        value = tuple(command)
        self.commands.append(value)
        if value[:2] == ("gh", "api") and value[-1].endswith("/reviews"):
            return self.reviews
        if value[:2] == ("gh", "api"):
            return self.pulls.pop(0)
        if value[:3] == ("gh", "pr", "checks"):
            return json.dumps([{"name": "Rotisserie CI", "state": "SUCCESS"}])
        return ""


def execute(runner: Runner, **overrides: object) -> None:
    arguments: dict[str, object] = {
        "scope": SCOPE,
        "repository": REPOSITORY,
        "pull_request": 12,
        "expected_head": HEAD,
        "enabled": True,
        "run": runner,
    }
    arguments.update(overrides)
    merge_canary(**arguments)  # type: ignore[arg-type]


def test_canary_revalidates_and_merges_the_exact_head() -> None:
    runner = Runner()
    execute(runner)
    assert runner.commands[-1][-3:] == ("--match-head-commit", HEAD, "--delete-branch")
    assert (
        "gh",
        "pr",
        "checks",
        "12",
        "--repo",
        REPOSITORY,
        "--required",
        "--json",
        "name,state",
    ) in runner.commands


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"enabled": False}, "kill switch"),
        ({"repository": "other/repository"}, "repository"),
        ({"expected_head": "not-a-sha"}, "invalid"),
    ],
)
def test_canary_rejects_disabled_or_out_of_scope_input(
    overrides: dict[str, object], message: str
) -> None:
    runner = Runner()
    with pytest.raises(CanaryMergeError, match=message):
        execute(runner, **overrides)
    assert runner.commands == []


def test_canary_rejects_forks_and_moved_heads_before_merge() -> None:
    with pytest.raises(CanaryMergeError, match="outside the canary scope"):
        execute(Runner(pulls=[pull(fork=True)]))

    moved = Runner(pulls=[pull(), pull(head="b" * 40)])
    with pytest.raises(CanaryMergeError, match="changed"):
        execute(moved)
    assert not any(command[:3] == ("gh", "pr", "merge") for command in moved.commands)


def test_canary_requires_independent_exact_head_approval() -> None:
    runner = Runner(
        reviews=json.dumps(
            [{"state": "APPROVED", "commit_id": HEAD, "user": {"login": "producer"}}]
        )
    )
    with pytest.raises(CanaryMergeError, match="independent approval"):
        execute(runner)
    assert not any(command[:3] == ("gh", "pr", "merge") for command in runner.commands)
