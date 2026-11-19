"""No-progress retirement must not discard active or newly changed work."""
from __future__ import annotations

import sys
import importlib
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
retire = importlib.import_module("factory_no_progress_retirement")
policy = importlib.import_module("factory_work_policy")

NOW = 1_794_485_000
HEAD = "a" * 40
PR = 4970
ISSUE = 4851


def pr(*, head: str = HEAD, owner: str = "factory:unowned") -> dict:
    return {
        "number": PR,
        "state": "open",
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "body": "Part of #4851.",
        "head": {
            "sha": head,
            "ref": "factory/45-4851-reading-orders",
            "repo": {"full_name": retire.REPO},
        },
        "base": {"ref": "main", "repo": {"full_name": retire.REPO}},
        "labels": [
            {"name": "factory"},
            {"name": owner},
            {"name": "factory:changes-requested"},
        ],
    }


def issue(*, owner: str = "factory:unowned") -> dict:
    return {
        "number": ISSUE,
        "state": "open",
        "body": "Migrate reading orders",
        "labels": [
            {"name": "factory"},
            {"name": owner},
            {"name": "factory:changes-requested"},
            {"name": "ralph-status:pending"},
            {"name": "rust"},
        ],
    }


def releases(
    *, sha: str = HEAD, reason: str = "repair-no-persisted-change-handoff",
    count: int = policy.FACTORY_NO_DIFF_RETRY_LIMIT, kind: str = "pr",
) -> list[dict]:
    return [
        {
            "body": (
                "<!-- comic-pile-factory-claim-released-v3:"
                f"{kind}-{PR if kind == 'pr' else ISSUE}:"
                f"opencode-free-model-factory-75:{NOW - 60 + n}:"
                f"{reason}:sha={sha}:stage=factory:changes-requested:"
                "conflicted=0 -->"
            )
        }
        for n in range(count)
    ]


@pytest.fixture(autouse=True)
def trusted_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "comment_is_trusted", lambda _: True)
    monkeypatch.setattr(retire, "comment_is_trusted", lambda _: True)


def test_exact_head_retry_limit_no_progress_attempts_qualify() -> None:
    assert retire.candidate_reason(
        pr(), issue(), releases(), now_epoch=NOW
    ) == (
        f"{policy.FACTORY_NO_DIFF_RETRY_LIMIT} verified no-progress repairs "
        f"at unchanged head {HEAD}"
    )


@pytest.mark.parametrize(
    "comments,changed_pr,changed_issue",
    [
        (releases(count=2), pr(), issue()),
        (releases(reason="transient-model-interruption"), pr(), issue()),
        (releases(reason="review-agent-failed"), pr(), issue()),
        (releases(sha="b" * 40), pr(), issue()),
        (releases(), pr(head="c" * 40), issue()),
        (releases(), pr(owner="factory:75"), issue()),
        (releases(), pr(), issue(owner="factory:75")),
        (releases(), pr(), {**issue(), "state": "closed"}),
        (releases(), {**pr(), "draft": True}, issue()),
        (releases(), {**pr(), "mergeable_state": "dirty"}, issue()),
        (releases(), {**pr(), "body": "<!-- factory-execution:manual-only -->"}, issue()),
    ],
)
def test_nonterminal_attempts_preserve_pr(
    comments: list[dict], changed_pr: dict, changed_issue: dict
) -> None:
    assert retire.candidate_reason(
        changed_pr, changed_issue, comments, now_epoch=NOW
    ) is None


def test_fork_and_unlinked_pr_never_retire() -> None:
    fork = pr()
    fork["head"]["repo"]["full_name"] = "other/repo"
    assert retire.candidate_reason(fork, issue(), releases(), now_epoch=NOW) is None
    unlink = pr()
    unlink["head"]["ref"] = "other/branch"
    assert retire.candidate_reason(unlink, issue(), releases(), now_epoch=NOW) is None


def test_issue_reset_only_clears_prior_issue_debt() -> None:
    old_issue = releases(kind="issue")
    old_pr = releases()
    reset = {
        "body": retire.retirement_marker(ISSUE, PR, HEAD, NOW - 5)
    }
    newer_issue = [
        {
            "body": (
                f"<!-- comic-pile-factory-claim-released-v3:issue-{ISSUE}:"
                f"opencode-free-model-factory-20:{NOW - 1}:"
                "no-persisted-change-handoff -->"
            )
        }
    ]
    records = policy.parse_no_diff_attempts_from_comments(
        [*old_issue, *old_pr, reset, *newer_issue], now_epoch=NOW
    )
    assert sum(a.kind == "pr" for a in records) == policy.FACTORY_NO_DIFF_RETRY_LIMIT
    assert sum(a.kind == "issue" for a in records) == 1


def test_untrusted_reset_cannot_clear_issue_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment = {"body": retire.retirement_marker(ISSUE, PR, HEAD, NOW - 5)}
    monkeypatch.setattr(policy, "comment_is_trusted", lambda c: c is not comment)
    records = policy.parse_no_diff_attempts_from_comments(
        [*releases(kind="issue"), comment], now_epoch=NOW
    )
    assert sum(a.kind == "issue" for a in records) == policy.FACTORY_NO_DIFF_RETRY_LIMIT


def test_retirement_writes_handoff_before_close_and_preserves_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(retire, "read_pr", lambda number: pr())
    monkeypatch.setattr(retire, "read_issue", lambda number: issue())
    monkeypatch.setattr(
        retire, "read_comments",
        lambda number: releases() if number == PR else [],
    )
    monkeypatch.setattr(
        retire, "restore_issue_intake",
        lambda payload: actions.append(("requeue", [str(payload["number"])])),
    )
    monkeypatch.setattr(
        retire, "gh",
        lambda args, **kwargs: actions.append(("gh", args)) or "",
    )
    result = retire.retire_pr(PR, now_epoch=NOW)
    assert result["status"] == "retired"
    assert actions[0][0] == "gh"
    assert actions[0][1][:2] == ["issue", "comment"]
    assert actions[1] == ("requeue", [str(ISSUE)])
    assert actions[2][1][:2] == ["pr", "close"]
    assert "--delete-branch" not in actions[2][1]
    assert HEAD in " ".join(actions[2][1])


def test_retirement_idempotent_issue_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    actions: list[list[str]] = []
    monkeypatch.setattr(retire, "read_pr", lambda _: pr())
    monkeypatch.setattr(retire, "read_issue", lambda _: issue())
    monkeypatch.setattr(
        retire, "read_comments",
        lambda number: (
            releases() if number == PR
            else [{"body": retire.retirement_marker(ISSUE, PR, HEAD, NOW - 5)}]
        ),
    )
    monkeypatch.setattr(retire, "restore_issue_intake", lambda _: None)
    monkeypatch.setattr(
        retire, "gh", lambda args, **kwargs: actions.append(args) or ""
    )
    assert retire.retire_pr(PR, now_epoch=NOW)["status"] == "retired"
    assert len(actions) == 1
    assert actions[0][:2] == ["pr", "close"]


def test_new_head_during_handoff_blocks_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = iter([pr(), pr(head="f" * 40)])
    actions: list[list[str]] = []
    monkeypatch.setattr(retire, "read_pr", lambda _: next(reads))
    monkeypatch.setattr(retire, "read_issue", lambda _: issue())
    monkeypatch.setattr(
        retire, "read_comments", lambda n: releases() if n == PR else []
    )
    monkeypatch.setattr(
        retire, "gh", lambda args, **kwargs: actions.append(args) or ""
    )
    monkeypatch.setattr(
        retire, "restore_issue_intake",
        lambda _: pytest.fail("new head cannot cause issue reset"),
    )
    assert retire.retire_pr(PR, now_epoch=NOW)["status"] == "changed-during-retirement"
    assert all(args[:2] != ["pr", "close"] for args in actions)


def test_reset_preserves_unrelated_issue_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        retire, "gh", lambda args, **kwargs: calls.append(kwargs["payload"]) or ""
    )
    retire.restore_issue_intake(issue())
    labels = calls[0]["labels"]
    assert "rust" in labels
    assert "ralph-status:pending" in labels
    assert "factory:unowned" in labels
    assert "factory:changes-requested" not in labels
