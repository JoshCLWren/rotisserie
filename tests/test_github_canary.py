"""Fail-closed tests for the manually dispatched self-dogfood merge canary."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from rotisserie.adapters.github.canary import (
    CanaryMergeError,
    CanaryScope,
    merge_canary,
    subprocess_runner,
)

REPOSITORY = "JoshCLWren/rotisserie"
HEAD = "a" * 40
SCOPE = CanaryScope(REPOSITORY, 9, "rotisserie/canary-9")


def marker(
    *,
    head: str = HEAD,
    reviewer: str = "opencode-review",
    producer: str = SCOPE.producer,
    verdict: str = "approve",
) -> str:
    return (
        "<!-- rotisserie-semantic-review-v1:"
        f"pr-12:head-{head}:reviewer-{reviewer}:producer-{producer}:"
        f"verdict-{verdict} -->"
    )


def pull(
    *,
    head: str = HEAD,
    fork: bool = False,
    state: str = "open",
    draft: bool = False,
    body: str = "Closes #9",
    base: str = "main",
    base_repository: str = REPOSITORY,
) -> str:
    return json.dumps(
        {
            "state": state,
            "draft": draft,
            "body": body,
            "user": {"login": "producer"},
            "base": {"ref": base, "repo": {"full_name": base_repository}},
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
    comments: str = field(default_factory=lambda: json.dumps([{"body": marker()}]))
    checks: str = field(
        default_factory=lambda: json.dumps(
            [
                {"name": "Python 3.12", "state": "SUCCESS"},
                {"name": "Python 3.13", "state": "SUCCESS"},
                {"name": "Python 3.14", "state": "SUCCESS"},
            ]
        )
    )
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, command: Sequence[str]) -> str:
        value = tuple(command)
        self.commands.append(value)
        if value[:2] == ("gh", "api") and value[-1].endswith("/comments"):
            return self.comments
        if value[:2] == ("gh", "api"):
            return self.pulls.pop(0)
        if value[:3] == ("gh", "pr", "checks"):
            return self.checks
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


@pytest.mark.parametrize(
    "review_marker",
    [
        marker(head="b" * 40),
        marker(reviewer=SCOPE.producer),
        marker(producer="other-producer"),
        marker(verdict="repair"),
        marker(verdict="reject"),
        "ordinary review prose is not controller evidence",
    ],
)
def test_canary_requires_independent_exact_head_semantic_evidence(
    review_marker: str,
) -> None:
    runner = Runner(comments=json.dumps([{"body": review_marker}]))
    with pytest.raises(CanaryMergeError, match="semantic review evidence"):
        execute(runner)
    assert not any(command[:3] == ("gh", "pr", "merge") for command in runner.commands)


@pytest.mark.parametrize("state", ["PENDING", "FAILURE", "ERROR", "CANCELLED", "SKIPPED"])
def test_canary_rejects_every_unsuccessful_required_check(state: str) -> None:
    checks = [
        {"name": version, "state": state if version == "Python 3.13" else "SUCCESS"}
        for version in ("Python 3.12", "Python 3.13", "Python 3.14")
    ]
    runner = Runner(checks=json.dumps(checks))
    with pytest.raises(CanaryMergeError, match="unsuccessful required CI"):
        execute(runner)
    assert not any(command[:3] == ("gh", "pr", "merge") for command in runner.commands)


def test_canary_rejects_missing_or_unexpected_required_checks() -> None:
    runner = Runner(checks=json.dumps([{"name": "Python 3.12", "state": "SUCCESS"}]))
    with pytest.raises(CanaryMergeError, match="complete required CI set"):
        execute(runner)


@pytest.mark.parametrize(
    "candidate",
    [
        pull(state="closed"),
        pull(draft=True),
        pull(base="release"),
        pull(base_repository="other/repository"),
    ],
)
def test_canary_rejects_wrong_pull_request_identity(candidate: str) -> None:
    with pytest.raises(CanaryMergeError, match="outside the canary scope"):
        execute(Runner(pulls=[candidate]))


def test_canary_rejects_non_closing_body() -> None:
    with pytest.raises(CanaryMergeError, match="does not close"):
        execute(Runner(pulls=[pull(body="Related to #9")]))


def test_subprocess_failures_become_clean_canary_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(CanaryMergeError, match="failed closed"):
        subprocess_runner(("gh", "pr", "checks"))
