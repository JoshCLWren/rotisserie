"""Parked PRs wait on a person, so they never block issue intake below the parked cap.

The 2026-11-25 ComicPile stall: the loop breaker parked every open factory PR.
They filled the review backlog limit of 6, so no issue could start, and no
worker could drain them because they were held.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import factory_dispatch_holds as holds
import factory_self_heal as heal
import factory_self_heal_rules as rules
import pytest
from factory_work_policy import (
    LOOP_HOLD_KEY,
    build_candidates,
    completion_backlog,
    explain_issue_report,
    factory_parked_count,
    factory_pr_wip_count,
    intake_backlog_count,
    parked_pr_limit,
)

from tests import turso_support as support

adopter = importlib.import_module("factory_rotisserie_adopter")
HEAD = "aad6557624bb4d843213ced45f23273f4acd7029"
LIMIT = 6


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FACTORY_PARKED_PR_LIMIT", raising=False)
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def pr(number: int, stage: str = "factory:changes-requested") -> dict[str, object]:
    return {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "labels": [{"name": name} for name in ("factory", "factory:unowned", stage)],
        "headRefName": f"factory/20-{9000 + number}-test",
        "headRefOid": HEAD,
        "body": "Worker: opencode-free-model-factory-20",
        "createdAt": "2026-11-24T00:00:00Z",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }


def issue(number: int, *extra: str) -> dict[str, object]:
    return {
        "number": number,
        "state": "OPEN",
        "title": f"Issue {number}",
        "labels": [{"name": name} for name in ("factory", "factory:unowned", *extra)],
        "createdAt": "2026-11-20T00:00:00Z",
    }


def park(repo: str, number: int, stage: str = "factory:changes-requested") -> None:
    ref = f"{stage.removeprefix('factory:')}-{HEAD[:12]}"
    entry = heal.event(kind=rules.LOOP_PARKED, pr=number, head=HEAD, ref=ref, text="t", stage=stage)
    assert heal.LedgerStore(repo).record([entry]) == 1


def starts(issues: list[dict[str, object]], prs: list[dict[str, object]]) -> set[int]:
    found = build_candidates(issues, prs, review_backlog_limit=LIMIT)
    return {c.number for c in found if c.kind == "issue"}


TONIGHT = [5674, 5737, 5745, 5759, 5767, 5768]


def test_six_parked_prs_at_a_limit_of_six_let_intake_proceed(env) -> None:
    _, repo = env
    prs = [pr(number) for number in TONIGHT]
    for number in TONIGHT:
        park(repo, number)
    assert starts([issue(41)], prs) == set()
    stamped = holds.stamp(repo, prs)
    assert {row[LOOP_HOLD_KEY] for row in stamped} == {"held: loop-parked"}
    assert factory_parked_count(stamped) == 6
    assert intake_backlog_count(stamped, LIMIT) == 0
    assert completion_backlog(stamped) == []
    assert factory_pr_wip_count(stamped) == 0
    assert starts([issue(41)], stamped) == {41}
    report = explain_issue_report(
        41,
        issue(41),
        issues=[issue(41)],
        prs=stamped,
        no_diff_attempts={},
        intake=None,
        open_blocker=None,
        review_backlog_limit=LIMIT,
    )
    gates = report["dynamic_gates"]
    assert (gates["review_backlog"], gates["parked_prs"], gates["parked_pr_limit"]) == (0, 6, 12)


def test_the_adopter_passes_rotisserie_the_backlog_without_parked_prs(env) -> None:
    _, repo = env
    for number in TONIGHT:
        park(repo, number)
    stamped = holds.stamp(repo, [pr(number) for number in TONIGHT])
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_k: object) -> object:
        seen.append(argv)
        raise OSError("stop after the command is built")

    control = {
        "stage": "expanded",
        "lane": {"name": "issue-intake", "subjects": ["label:factory:unowned"]},
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(adopter.subprocess, "run", fake_run)
        patch.setattr(adopter, "repository", lambda: "acme/widgets")
        with pytest.raises(OSError):
            adopter.intake_verdicts(
                control,
                [issue(41)],
                stamped,
                revision="a" * 40,
                now=1,
                command=["rotisserie"],
                review_backlog_limit=LIMIT,
            )
    argv = seen[0]
    assert argv[argv.index("--completion-backlog") + 1] == "0"


def test_the_parked_cap_still_stops_intake(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, repo = env
    numbers = list(range(6000, 6000 + 2 * LIMIT))
    for number in numbers:
        park(repo, number)
    stamped = holds.stamp(repo, [pr(number) for number in numbers])
    assert parked_pr_limit(LIMIT) == 12
    assert intake_backlog_count(stamped, LIMIT) == LIMIT
    assert starts([issue(41)], stamped) == set()
    assert starts([issue(42, "main-breakage")], stamped) == {42}
    monkeypatch.setenv("FACTORY_PARKED_PR_LIMIT", "20")
    assert starts([issue(41)], stamped) == {41}


def test_a_new_head_unparks_and_counts_again(env) -> None:
    _, repo = env
    for number in TONIGHT:
        park(repo, number)
    moved = [{**pr(number), "headRefOid": "b" * 40} for number in TONIGHT]
    stamped = holds.stamp(repo, moved)
    assert factory_parked_count(stamped) == 0
    assert starts([issue(41)], stamped) == set()


def test_a_store_failure_marks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import factory_state_operations

    def broken(*_a: object, **_k: object) -> dict[int, str]:
        raise RuntimeError("store down")

    monkeypatch.setattr(factory_state_operations, "holds", broken)
    prs = [pr(number) for number in TONIGHT]
    assert holds.stamp("acme/widgets", prs) == prs
