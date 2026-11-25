"""The label doctor settles duplicate, needs-split and gate-rejected labels from what it reads."""

from __future__ import annotations

from typing import Any

from test_label_doctor_parked import HEAD, Hub, repair  # type: ignore[import-not-found]

from rotisserie.factory_console import runtime

DUPLICATE = "factory:duplicate"
NEEDS_SPLIT = "factory:needs-split"
GATE_REJECTED = "factory:gate-rejected"
BOT = {"login": "github-actions[bot]", "type": "Bot"}


def rejected_marker(pr: int, head: str) -> str:
    prefix = runtime.module("factory_adopter").MARKER_PREFIX
    return f"<!-- {prefix}-factory-acceptance-rejected-v1:pr-{pr}:head-{head} -->"


def pull(number: int, body: str, head: str = HEAD, *labels: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": "t",
        "body": body,
        "head": {"sha": head, "ref": f"local/{number}"},
        "labels": [{"name": n} for n in ("factory", *labels)],
    }


def github_with(prs: dict[int, set[str]], pulls: list[dict[str, Any]]) -> Hub:
    github = Hub(
        {n: {"factory", "factory:unowned", "factory:review", *names} for n, names in prs.items()}
    )
    for number in prs:
        github.kinds[number] = "pr"
    github.pulls = pulls
    github.search_hits = []
    return github


def test_the_doctor_labels_the_newer_of_two_prs_for_one_issue() -> None:
    github = github_with(
        {5768: set(), 5775: set()},
        [pull(5768, "Part of #5695."), pull(5775, "Part of #5695.")],
    )
    repair(github)
    assert ("add", 5775, DUPLICATE) in github.writes
    assert ("add", 5768, DUPLICATE) not in github.writes
    assert ("define", 0, DUPLICATE) in github.writes


def test_the_doctor_drops_duplicate_when_one_pr_is_left() -> None:
    github = github_with({5775: {DUPLICATE}}, [pull(5775, "Part of #5695.", HEAD, DUPLICATE)])
    repair(github)
    assert ("remove", 5775, DUPLICATE) in github.writes


def test_the_doctor_sets_and_clears_needs_split_on_issues() -> None:
    github = github_with({}, [])
    github.items[4335] = {"factory", "factory:unowned"}
    github.items[7] = {"factory", "factory:unowned", NEEDS_SPLIT}
    github.bodies = {4335: "x" * 25_713, 7: "A small task."}
    repair(github)
    assert ("add", 4335, NEEDS_SPLIT) in github.writes
    assert ("remove", 7, NEEDS_SPLIT) in github.writes


def test_the_doctor_drops_gate_rejected_after_a_new_head() -> None:
    github = github_with({12: {GATE_REJECTED}}, [pull(12, "Closes #1.", "e" * 40, GATE_REJECTED)])
    github.comments[12] = [{"user": BOT, "body": rejected_marker(12, HEAD)}]
    repair(github)
    assert ("remove", 12, GATE_REJECTED) in github.writes


def test_the_doctor_keeps_gate_rejected_at_the_rejected_head() -> None:
    github = github_with({12: {GATE_REJECTED}}, [pull(12, "Closes #1.", HEAD, GATE_REJECTED)])
    github.comments[12] = [{"user": BOT, "body": rejected_marker(12, HEAD)}]
    repair(github)
    assert ("remove", 12, GATE_REJECTED) not in github.writes
