"""No duplicate implement runs for an issue with an open slice PR, and no epic as one task."""

from __future__ import annotations

from typing import Any

import factory_issue_intake_guard as guard
import pytest
from factory_work_policy import build_candidates

# Measured on ComicPile 2026-11-25: the largest body of 33 recently merged issues
# was 11,173 characters (median 2,263); epic #4335 was 25,713.
LARGEST_MERGED_BODY = 11_173
EPIC_4335_BODY = 25_713


def issue(number: int, body: str = "Fix it.", *labels: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "state": "OPEN",
        "labels": [{"name": n} for n in ("factory", "factory:unowned", *labels)],
        "createdAt": "2026-11-20T00:00:00Z",
    }


def slice_pr(number: int, issue_number: int, *, branch: str = "", body: str = "") -> dict[str, Any]:
    return {
        "number": number,
        "state": "OPEN",
        "headRefName": branch or f"local/slice-{number}",
        "headRefOid": "a" * 40,
        "body": body or f"Part of #{issue_number}.\n\nDelivers the first slice.",
        "title": "A slice",
        "labels": [],
    }


def issue_numbers(
    issues: list[dict[str, Any]], prs: list[dict[str, Any]], **kwargs: Any
) -> set[int]:
    found = build_candidates(issues, prs, include_backpressured_issues=True, **kwargs)
    return {c.number for c in found if c.kind == "issue"}


def test_an_open_part_of_pr_holds_its_issue() -> None:
    assert issue_numbers([issue(5692), issue(7)], [slice_pr(5737, 5692)]) == {7}


def test_an_open_checkpoint_branch_holds_its_issue() -> None:
    pr = slice_pr(5768, 5695, branch="factory/work-checkpoints/issue-5695/model/45535904578")
    pr["body"] = "Model: x"
    assert issue_numbers([issue(5695)], [pr]) == set()


def test_a_targeted_run_override_or_claim_is_not_held() -> None:
    prs = [slice_pr(5737, 5692)]
    assert issue_numbers([issue(5692)], prs, intake_holds=False) == {5692}
    assert issue_numbers([issue(5692, "Fix it.", "factory:override")], prs) == {5692}
    assert issue_numbers([issue(5692, "Fix it.", "factory:claimed")], prs) == {5692}


def test_merged_slices_do_not_hold_the_issue_but_become_context() -> None:
    assert issue_numbers([issue(5751)], []) == {5751}
    merged = [
        {"number": 5756, "title": "Read now on roll", "body": "Part of #5751."},
        {"number": 9, "title": "Other", "body": "Part of #12."},
    ]
    line = guard.merged_context(5751, merged)
    assert "#5756 (Read now on roll)" in line and "#9" not in line
    assert guard.merged_context(5751, []) == ""


def test_the_size_limit_keeps_every_merged_issue_and_holds_the_epic() -> None:
    assert LARGEST_MERGED_BODY < guard.BODY_LIMIT < EPIC_4335_BODY
    issues = [issue(5695, "x" * LARGEST_MERGED_BODY), issue(4335, "x" * EPIC_4335_BODY)]
    assert issue_numbers(issues, []) == {5695}


def test_an_epic_umbrella_or_tracking_label_holds_the_issue() -> None:
    for label in ("epic", "Umbrella", "tracking"):
        assert issue_numbers([issue(4400, "Small body.", label)], []) == set()


@pytest.fixture(autouse=True)
def fresh_requests() -> None:
    guard._ASKED.clear()


def test_a_split_request_is_posted_once_with_a_label() -> None:
    writes: list[tuple[str, object]] = []
    big = issue(4335, "x" * EPIC_4335_BODY)
    asked = guard.request_splits([big, issue(7)], lambda path, body: writes.append((path, body)))
    assert asked == [4335]
    paths = [path for path, _ in writes]
    assert paths == ["labels", "issues/4335/labels", "issues/4335/comments"]
    comment = str(writes[-1][1]["body"])  # type: ignore[index]
    assert "25,713 characters, over the 16,000-character limit" in comment
    assert "creates the child issues from the validated plan" in comment
    labelled = issue(4335, "x" * EPIC_4335_BODY, guard.NEEDS_SPLIT)
    assert guard.request_splits([labelled], lambda *_: writes.append(("again", None))) == []


def test_a_second_pool_pass_in_one_job_does_not_ask_again() -> None:
    writes: list[str] = []
    rows = [issue(4335, "x" * EPIC_4335_BODY)]
    assert guard.request_splits(rows, lambda path, _: writes.append(path)) == [4335]
    assert guard.request_splits(rows, lambda path, _: writes.append(path)) == []
    assert writes.count("issues/4335/comments") == 1


def test_a_pass_from_a_stale_listing_reads_the_live_label_and_does_not_ask_again() -> None:
    writes: list[str] = []
    stale = [issue(5743, "Small body.", "umbrella")]
    live = issue(5743, "Small body.", "umbrella", guard.NEEDS_SPLIT)
    asked = guard.request_splits(stale, lambda path, _: writes.append(path), read=lambda _: live)
    assert asked == [] and writes == []
    fresh = issue(5743, "Small body.", "umbrella")
    guard._ASKED.clear()
    assert guard.request_splits(stale, lambda path, _: writes.append(path), read=lambda _: fresh)
    assert writes.count("issues/5743/comments") == 1


def test_only_issues_the_pool_would_dispatch_are_asked() -> None:
    owned = issue(1, "x" * EPIC_4335_BODY)
    owned["labels"] = [{"name": "factory"}, {"name": "factory:42"}]
    manual = issue(2, "<!-- factory-execution:manual-only -->\n" + "x" * EPIC_4335_BODY)
    blocked = issue(3, "x" * EPIC_4335_BODY, "factory:blocked")
    assert guard.request_splits([owned, manual, blocked], lambda *_: None) == []


def test_the_controller_holds_slices_only_on_the_pool_pass(monkeypatch: Any) -> None:
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "factory_work_controller_intake_guard", root / ".github/scripts/factory-work-controller.py"
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    seen: list[bool] = []
    asked: list[int] = []
    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(controller, "reconcile_contradictory_labels", lambda: [])
    monkeypatch.setattr(controller, "list_issues", lambda: [issue(5692)])
    monkeypatch.setattr(controller, "list_prs", lambda: [slice_pr(5737, 5692)])
    monkeypatch.setattr(controller, "load_no_diff_attempt_records", lambda **_: [])
    monkeypatch.setattr(
        controller,
        "build_candidates",
        lambda *_a, **k: seen.append(k["intake_holds"]) or [],
    )
    monkeypatch.setattr(
        controller.factory_issue_intake_guard,
        "request_splits_gh",
        lambda issues, repo: asked.append(len(issues)) or [],
    )
    monkeypatch.setattr(controller, "rotisserie_issue_intake", lambda *_a, **_k: None)
    monkeypatch.setenv("FACTORY_JEV_RANKING_FILE", "")
    controller.assign_from_pool("6")
    try:
        controller.assign_from_pool("6", target=("issue", 5692))
    except RuntimeError:
        pass
    assert seen == [True, False]
    assert asked == [1]
