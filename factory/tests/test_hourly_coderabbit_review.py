"""Deterministic tests for the hourly, quota-bounded Jev → CodeRabbit action."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "hourly_coderabbit_review", SCRIPTS / "hourly_coderabbit_review.py"
)
assert SPEC is not None and SPEC.loader is not None
review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = review
SPEC.loader.exec_module(review)

NOW = datetime(2026, 11, 2, 18, 32, tzinfo=UTC)
REPO = "JoshCLWren/comic-pile"


def pr(
    number: int, *, head: str | None = None, state: str = "OPEN",
    draft: bool = False, merge: str = "CLEAN", closed: str | None = None,
) -> dict[str, Any]:
    """Make one GitHub CLI-shaped PR snapshot without network dependencies."""
    return {
        "number": number,
        "state": state,
        "title": f"Fix important reader regression #{number}",
        "headRefOid": head or f"{number % 10}" * 40,
        "isDraft": draft,
        "mergeStateStatus": merge,
        "createdAt": "2026-10-31T12:00:00Z",
        "closedAt": closed,
        "labels": [{"name": "ralph-priority:medium"}],
        "statusCheckRollup": [{"name": "CI Summary", "conclusion": "SUCCESS"}],
    }


def decision(payload: dict[str, Any], _key: str) -> dict[str, Any]:
    """Jev's typed choice prefers the last candidate."""
    ids = list(payload["questions"]["review_target"]["criteria"])
    chosen = ids[-1]
    return {
        "model": review.MODEL,
        "answers": {"review_target": {
            "type": "choice",
            "choice": chosen,
            "confidence": 0.98,
            "probabilities": {
                item: 0.9 if item == chosen else 0.1 / (len(ids) - 1)
                for item in ids
            },
        }},
    }


def fake_api(
    pr_rows: list[dict[str, Any]],
    *, global_comments: list[dict[str, Any]] | None = None,
    submitted: dict[int, list[dict[str, Any]]] | None = None,
    per_pr_comments: dict[int, list[dict[str, Any]]] | None = None,
    change_head: bool = False,
) -> tuple[Any, list[tuple[str, str | None, str | None]]]:
    """Emulate only GitHub endpoints this workflow is authorized to access."""
    comments = global_comments or []
    reviews = submitted or {}
    request_comments = per_pr_comments or {}
    posts: list[tuple[str, str | None, str | None]] = []
    by_number = {row["number"]: row for row in pr_rows}

    def get(
        path: str, *, token: str | None = None, body: str | None = None
    ) -> dict[str, object] | list[dict[str, object]]:
        """Return fixture API responses and capture attempted review posts."""
        if body is not None:
            posts.append((path, token, body))
            return {"id": 1}
        if "/issues/comments?" in path:
            return comments
        if "/pulls/" in path and "/reviews?" in path:
            return reviews.get(int(path.split("/pulls/")[1].split("/")[0]), [])
        if "/pulls/" in path:
            number = int(path.rsplit("/", 1)[1])
            row = by_number[number]
            return {
                "state": "open" if row["state"] == "OPEN" else "closed",
                "draft": row["isDraft"],
                "head": {
                    "sha": ("f" * 40 if change_head else row["headRefOid"])
                },
            }
        if "/issues/" in path and "/comments?" in path:
            number = int(path.split("/issues/")[1].split("/")[0])
            return request_comments.get(number, [])
        if "/commits/" in path:
            return {"commit": {"committer": {"date": "2026-11-02T15:00:00Z"}}}
        raise AssertionError(f"unexpected API request {path}")

    return get, posts


def test_jev_actively_chooses_a_nonfirst_pull_request() -> None:
    """Verify jev actively chooses a nonfirst pull request."""
    """The model choice, not the deterministic candidate order, is requested."""
    rows = [pr(101), pr(102)]
    getter, posts = fake_api(rows)
    result = review.perform(REPO, rows, NOW, "jev-key", "user-pat", getter, decision)
    assert "PR #102" in result
    assert posts == [
        (f"repos/{REPO}/issues/102/comments", "user-pat", "@coderabbitai review")
    ]


def test_code_rabbit_review_at_exact_head_is_ineligible() -> None:
    """Verify code rabbit review at exact head is ineligible."""
    rows = [pr(101), pr(102)]
    getter, posts = fake_api(rows, submitted={
        101: [{
            "user": {"login": "coderabbitai[bot]"},
            "commit_id": rows[0]["headRefOid"],
            "submitted_at": "2026-10-31T14:00:00Z",
        }]
    })
    result = review.perform(REPO, rows, NOW, "jev-key", "user-pat", getter, decision)
    assert "PR #102" in result
    assert len(posts) == 1


def test_recent_actual_review_spends_shared_hourly_slot() -> None:
    """Verify recent actual review spends shared hourly slot."""
    rows = [pr(101), pr(102)]
    getter, posts = fake_api(rows, submitted={
        101: [{
            "user": {"login": "coderabbitai[bot]"},
            "commit_id": rows[0]["headRefOid"],
            "submitted_at": "2026-11-02T18:15:00Z",
        }]
    })
    result = review.perform(REPO, rows, NOW, "jev-key", "user-pat", getter, decision)
    assert "last 60 minutes" in result
    assert posts == []


def test_recent_manual_review_command_uses_the_quota() -> None:
    """Verify recent manual review command uses the quota."""
    rows = [pr(101), pr(102)]
    getter, posts = fake_api(rows, global_comments=[{
        "created_at": "2026-11-02T18:01:00Z",
        "user": {"login": "JoshCLWren"},
        "body": "@coderabbitai review",
    }])
    result = review.perform(REPO, rows, NOW, "jev-key", "user-pat", getter, decision)
    assert "last 60 minutes" in result
    assert posts == []


def test_bot_quoting_the_command_does_not_consume_quota() -> None:
    """Verify bot quoting the command does not consume quota."""
    rows = [pr(101)]
    getter, posts = fake_api(rows, global_comments=[{
        "created_at": "2026-11-02T18:01:00Z",
        "user": {"login": "coderabbitai[bot]"},
        "body": "Command: @coderabbitai review",
    }])
    assert "Requested CodeRabbit" in review.perform(
        REPO, rows, NOW, "jev-key", "user-pat", getter, decision
    )
    assert len(posts) == 1


def test_already_requested_same_head_is_not_retried() -> None:
    """Verify already requested same head is not retried."""
    rows = [pr(101)]
    getter, posts = fake_api(rows, per_pr_comments={101: [{
        "body": "@coderabbitai review",
        "created_at": "2026-11-02T16:10:00Z",
    }]})
    result = review.perform(REPO, rows, NOW, "jev-key", "user-pat", getter, decision)
    assert "already been requested" in result
    assert posts == []


def test_new_head_can_be_reviewed_after_older_command() -> None:
    """Verify new head can be reviewed after older command."""
    rows = [pr(101)]
    getter, posts = fake_api(rows, per_pr_comments={101: [{
        "body": "@coderabbitai review",
        "created_at": "2026-11-02T14:10:00Z",
    }]})
    assert "Requested CodeRabbit" in review.perform(
        REPO, rows, NOW, "jev-key", "user-pat", getter, decision
    )
    assert len(posts) == 1


@pytest.mark.parametrize("draft,merge,state", [
    (True, "CLEAN", "OPEN"),
    (False, "DIRTY", "OPEN"),
    (False, "CLEAN", "MERGED"),
])
def test_nonreviewable_prs_are_excluded(
    draft: bool, merge: str, state: str
) -> None:
    """Reject draft, conflicted, and closed PRs before invoking the model."""
    row = pr(101, draft=draft, merge=merge, state=state)
    if state == "MERGED":
        row["closedAt"] = "2026-11-02T18:15:00Z"
    getter, posts = fake_api([row])
    result = review.perform(REPO, [row], NOW, "jev-key", "user-pat", getter, decision)
    assert "no eligible PR" in result
    assert posts == []


def test_head_movement_after_jev_selection_blocks_command() -> None:
    """Verify head movement after jev selection blocks command."""
    rows = [pr(101), pr(102)]
    getter, posts = fake_api(rows, change_head=True)
    result = review.perform(REPO, rows, NOW, "jev-key", "user-pat", getter, decision)
    assert "head or reviewability changed" in result
    assert posts == []


def test_no_user_token_or_jev_key_never_posts() -> None:
    """Verify no user token or jev key never posts."""
    rows = [pr(101), pr(102)]
    getter, posts = fake_api(rows)
    assert "not configured" in review.perform(
        REPO, rows, NOW, "", "user-pat", getter, decision
    )
    assert "not configured" in review.perform(
        REPO, rows, NOW, "jev-key", "", getter, decision
    )
    assert posts == []


def test_low_probability_jev_choice_does_not_spend_review() -> None:
    """Verify low probability jev choice does not spend review."""
    rows = [pr(101), pr(102)]
    getter, posts = fake_api(rows)

    def ambiguous(payload: dict[str, Any], key: str) -> dict[str, Any]:
        answer = decision(payload, key)
        answer["answers"]["review_target"]["probabilities"] = {
            "pr-101": 0.51,
            "pr-102": 0.49,
        }
        return answer

    result = review.perform(REPO, rows, NOW, "jev-key", "user-pat", getter, ambiguous)
    assert "not select a sufficiently confident" in result
    assert posts == []


def test_invalid_jev_response_fails_without_fallback_comment() -> None:
    """Verify invalid jev response fails without fallback comment."""
    rows = [pr(101), pr(102)]
    getter, posts = fake_api(rows)
    result = review.perform(
        REPO, rows, NOW, "jev-key", "user-pat", getter,
        lambda _payload, _key: {"model": "invalid", "answers": {}},
    )
    assert "decision invalid" in result
    assert posts == []


def test_incomplete_comment_history_fails_closed() -> None:
    """Verify incomplete comment history fails closed."""
    rows = [pr(101)]

    def broken(
        path: str, *, token: str | None = None, body: str | None = None
    ) -> list[dict[str, str]]:
        """Simulate a GitHub comment history that exhausts the pagination limit."""
        if "/issues/comments?" in path:
            return [{"created_at": "2026-11-02T18:00:00Z", "body": ""}] * 100
        raise AssertionError("should not attempt the write")

    with pytest.raises(ValueError, match="pagination"):
        review.perform(REPO, rows, NOW, "jev-key", "user-pat", broken, decision)


def test_shortlist_favors_priority_then_green_ci() -> None:
    """Verify shortlist favors priority then green ci."""
    rows = [pr(101), pr(102), pr(103)]
    rows[2]["labels"] = [{"name": "priority:P0"}]
    rows[1]["statusCheckRollup"] = [{"name": "CI Summary", "conclusion": "FAILURE"}]
    selected = review.choices(rows, {101: [], 102: [], 103: []}, NOW)
    assert [item.number for item in selected] == [103, 101, 102]


def test_jev_review_uses_existing_dispatcher_driven_merge_drain() -> None:
    """No independent cron or failure coupling; existing hourly quota survives."""
    workflows = SCRIPTS.parent / "workflows"
    drain = (workflows / "factory-ready-merge-drain.yml").read_text(encoding="utf-8")
    dispatcher = (workflows / "fixed-model-factory-dispatch.yml").read_text(
        encoding="utf-8"
    )
    assert not (workflows / "hourly-jev-coderabbit-review.yml").exists()
    assert "gh workflow run factory-ready-merge-drain.yml" in dispatcher
    step_name = "      - name: Ask Jev for CodeRabbit review on the existing drain cadence"
    assert drain.count(step_name) == 1
    step = drain.split(step_name, 1)[1].split("\n      - name:", 1)[0]
    assert "continue-on-error: true" in step
    assert "GH_TOKEN: ${{ github.token }}" in step
    assert "OPENROUTER_JEV_API_KEY: ${{ secrets.OPENROUTER_JEV_API_KEY }}" in step
    assert "PR_REBASE_TOKEN: ${{ secrets.PR_REBASE_TOKEN }}" in step
    assert "gh pr list " not in step
    assert "--prs " not in step
    assert "--repo" in step
    assert "hourly_coderabbit_review.py" in step
    # Preserve the existing redundant merge drain fallback, not another timer.
    assert "cron: '2-57/15 * * * *'" in drain
    assert drain.count("    - cron:") == 1


def test_rest_snapshot_reads_open_and_recent_closed_with_check_runs() -> None:
    """Load REST data with accurate CI and the shared recent-closure window."""
    now = NOW
    calls: list[str] = []
    open_row = {
        "number": 101, "state": "open", "draft": False,
        "head": {"sha": "a" * 40}, "title": "review me",
        "created_at": "2026-11-01T17:00:00Z",
        "updated_at": "2026-11-02T18:25:00Z", "closed_at": None,
        "labels": [{"name": "priority:P1"}],
    }
    closed_row = {
        **open_row, "number": 102, "state": "closed",
        "head": {"sha": "b" * 40},
        "closed_at": "2026-11-02T18:20:00Z",
    }
    old_row = {
        **closed_row, "number": 103, "head": {"sha": "c" * 40},
        "updated_at": "2026-11-02T14:00:00Z",
        "closed_at": "2026-11-02T14:00:00Z",
    }

    def rest(path: str, *, token: str | None = None, body: str | None = None) -> object:
        """Emulate GitHub's REST PR listing and check-run response."""
        calls.append(path)
        if "pulls?state=open" in path:
            return [open_row]
        if "pulls?state=closed" in path:
            return [closed_row, old_row]
        if "/check-runs?" in path:
            return {"total_count": 1, "check_runs": [
                {"name": "CI Summary", "conclusion": "success", "status": "completed"}
            ]}
        raise AssertionError(f"unexpected REST call: {path}")

    snapshots = review.rest_pr_snapshots(REPO, now, rest)
    assert [row["number"] for row in snapshots] == [101, 102]
    assert snapshots[0]["state"] == "OPEN"
    assert snapshots[0]["headRefOid"] == "a" * 40
    assert review.ci_state(snapshots[0]) == "passing"
    assert snapshots[1]["state"] == "CLOSED"
    assert snapshots[1]["closedAt"] == "2026-11-02T18:20:00Z"
    assert len([path for path in calls if "/check-runs?" in path]) == 1
    assert all("graphql" not in path.lower() for path in calls)


def test_rest_snapshot_rejects_incomplete_checks() -> None:
    """Incomplete check history must not quietly influence a review decision."""
    row = {
        "number": 101, "state": "open", "draft": False,
        "head": {"sha": "a" * 40}, "title": "review me",
        "created_at": "2026-11-01T17:00:00Z",
        "updated_at": "2026-11-02T18:25:00Z",
        "labels": [], "closed_at": None,
    }

    def rest(path: str, *, token: str | None = None, body: str | None = None) -> object:
        """Return a truncated REST check response."""
        if "pulls?state=open" in path:
            return [row]
        if "/check-runs?" in path:
            return {"total_count": 101, "check_runs": []}
        raise AssertionError(f"unexpected REST call: {path}")

    with pytest.raises(ValueError, match="incomplete REST PR check history"):
        review.rest_pr_snapshots(REPO, NOW, rest)


def test_rest_snapshot_paginates_open_prs() -> None:
    """Avoid silently dropping older but still open PRs from candidate selection."""
    def make_row(number: int) -> dict[str, Any]:
        """Construct a REST open PR response."""
        return {
            "number": number, "state": "open", "draft": False,
            "head": {"sha": f"{number:040x}"}, "title": f"PR {number}",
            "created_at": "2026-11-01T17:00:00Z",
            "updated_at": "2026-11-02T18:25:00Z",
            "labels": [], "closed_at": None,
        }

    def rest(path: str, *, token: str | None = None, body: str | None = None) -> object:
        """Return 100 open PRs and one additional older open PR."""
        if "pulls?state=open" in path and path.endswith("&page=1"):
            return [make_row(i) for i in range(1, 101)]
        if "pulls?state=open" in path and path.endswith("&page=2"):
            return [make_row(101)]
        if "pulls?state=closed" in path:
            return []
        if "/check-runs?" in path:
            return {"total_count": 0, "check_runs": []}
        raise AssertionError(f"unexpected REST call: {path}")

    snapshots = review.rest_pr_snapshots(REPO, NOW, rest)
    assert len(snapshots) == 101
    assert snapshots[-1]["number"] == 101

