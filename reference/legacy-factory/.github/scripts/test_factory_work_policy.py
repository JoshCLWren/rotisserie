#!/usr/bin/env python3
"""Regression coverage for completion-aware factory queue policy."""
from __future__ import annotations

import unittest

from factory_work_policy import (
    FACTORY_NO_DIFF_RETRY_LIMIT,
    FACTORY_NO_DIFF_RETRY_RESET_SECONDS,
    Candidate,
    build_candidates,
    no_diff_attempts_from_comments,
    order_candidates_for_worker,
    parse_no_diff_attempts_from_comments,
)


def comment(body: str, association: str = "OWNER") -> dict[str, str]:
    return {"body": body, "author_association": association}


def labels(*names: str) -> list[dict[str, str]]:
    return [{"name": name} for name in names]


def pr_no_diff_marker(
    number: int,
    epoch: int,
    *,
    sha: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    stage: str = "factory:changes-requested",
    conflicted: bool = False,
    worker: str = "worker",
) -> str:
    return (
        "<!-- comic-pile-factory-claim-released-v3:"
        f"pr-{number}:{worker}:{epoch}:repair-no-persisted-change-handoff"
        f":sha={sha}:stage={stage}:conflicted={int(conflicted)} -->"
    )


def factory_pr(
    number: int,
    *,
    stage: str = "factory:review",
    worker: int = 20,
    linked_issue: int | None = None,
    mergeable: str = "MERGEABLE",
    merge_state: str = "CLEAN",
    owner: str = "factory:unowned",
    branch: str | None = None,
    head: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
) -> dict[str, object]:
    issue = linked_issue if linked_issue is not None else 9000 + number
    return {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "labels": labels("factory", owner, stage),
        "headRefName": branch or f"factory/{worker}-{issue}-test",
        "headRefOid": head,
        "body": f"Worker: opencode-free-model-factory-{worker}",
        "createdAt": "2026-08-17T00:00:00Z",
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
    }


def issue(number: int, *extra_labels: str) -> dict[str, object]:
    return {
        "number": number,
        "state": "OPEN",
        "title": f"Issue {number}",
        "labels": labels("factory", "factory:unowned", *extra_labels),
        "createdAt": "2026-08-17T00:00:00Z",
    }


class CompletionAwareOrderingTests(unittest.TestCase):
    def test_review_first_worker_prioritizes_review_before_repair(self) -> None:
        candidates = [
            Candidate("pr", 1, 3, 0, "", stage="factory:review"),
            Candidate("pr", 2, 3, 0, "", stage="factory:changes-requested"),
            Candidate("pr", 3, 3, 0, "", stage="factory:ci"),
            Candidate("pr", 4, 3, 0, "", stage="factory:review", conflicted=True),
        ]
        self.assertEqual(
            [item.number for item in order_candidates_for_worker(candidates, "6")],
            [1, 4, 3, 2],
        )

    def test_build_candidates_marks_github_conflict_state(self) -> None:
        candidates = build_candidates(
            [],
            [factory_pr(5, mergeable="CONFLICTING", merge_state="DIRTY")],
        )
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].conflicted)

    def test_dirty_merge_state_is_enough_to_mark_conflict(self) -> None:
        candidates = build_candidates(
            [],
            [factory_pr(5, mergeable="UNKNOWN", merge_state="DIRTY")],
        )
        self.assertTrue(candidates[0].conflicted)

    def test_existing_review_pr_survives_blocked_linked_issue(self) -> None:
        issue_target = issue(2128, "factory:blocked")
        review_pr = factory_pr(
            2138,
            linked_issue=2128,
            stage="factory:review",
            worker=68,
        )

        candidates = build_candidates([issue_target], [review_pr])

        self.assertEqual([(item.kind, item.number) for item in candidates], [("pr", 2138)])

    def test_non_review_worker_prefers_fresh_issue_to_review_queue(self) -> None:
        candidates = [
            Candidate("issue", 1, 3, 4, "2026-08-17T00:00:00Z"),
            Candidate(
                "pr",
                2,
                3,
                0,
                "2026-08-16T00:00:00Z",
                stage="factory:review",
                producer_worker="43",
            ),
        ]
        self.assertEqual(order_candidates_for_worker(candidates, "9")[0].number, 1)

    def test_review_capacity_worker_still_prefers_review_pr(self) -> None:
        candidates = [
            Candidate("issue", 1, 3, 4, "2026-08-17T00:00:00Z"),
            Candidate(
                "pr",
                2,
                3,
                0,
                "2026-08-16T00:00:00Z",
                stage="factory:review",
                producer_worker="43",
            ),
        ]
        self.assertEqual(order_candidates_for_worker(candidates, "6")[0].number, 2)

    def test_review_first_worker_orders_review_then_ci_then_changes(self) -> None:
        candidates = [
            Candidate("pr", 1, 3, 0, "", stage="factory:review"),
            Candidate("pr", 2, 3, 0, "", stage="factory:changes-requested"),
            Candidate("pr", 3, 3, 0, "", stage="factory:ci"),
        ]
        self.assertEqual(
            [item.number for item in order_candidates_for_worker(candidates, "6")],
            [1, 3, 2],
        )

    def test_non_review_worker_preserves_fresh_issue_capacity(self) -> None:
        candidates = [
            Candidate("issue", 1, 1, 4, "", stage="factory:building"),
            Candidate("pr", 2, 5, 0, "", stage="factory:changes-requested"),
        ]
        self.assertEqual(order_candidates_for_worker(candidates, "9")[0].number, 1)

    def test_producer_cannot_semantically_review_own_pr(self) -> None:
        candidates = [
            Candidate(
                "pr",
                2,
                3,
                0,
                "",
                stage="factory:review",
                producer_worker="9",
            ),
            Candidate("issue", 1, 3, 0, ""),
        ]
        self.assertEqual(order_candidates_for_worker(candidates, "9")[0].number, 1)


class RetryBudgetTests(unittest.TestCase):
    def test_no_diff_history_counts_pr_repair_markers(self) -> None:
        comments = [
            {
                "author_association": "OWNER",
                "body": (
                    "<!-- comic-pile-factory-claim-released-v3:pr-31:worker:"
                    "1999999999:repair-no-persisted-change-handoff -->"
                ),
            }
        ]
        self.assertEqual(
            no_diff_attempts_from_comments(comments, now_epoch=2_000_000_000),
            {31: 1},
        )

    def test_no_diff_issue_retries_until_budget_is_exhausted(self) -> None:
        target = issue(31)
        below_budget = build_candidates(
            [target],
            [],
            no_diff_attempts_by_issue={31: FACTORY_NO_DIFF_RETRY_LIMIT - 1},
        )
        exhausted = build_candidates(
            [target],
            [],
            no_diff_attempts_by_issue={31: FACTORY_NO_DIFF_RETRY_LIMIT},
        )
        self.assertTrue(any(item.kind == "issue" and item.number == 31 for item in below_budget))
        self.assertFalse(any(item.kind == "issue" and item.number == 31 for item in exhausted))

    def test_no_diff_pr_retries_until_budget_is_exhausted(self) -> None:
        target = factory_pr(31, stage="factory:changes-requested")
        below_budget = build_candidates(
            [],
            [target],
            no_diff_attempts_by_issue={31: FACTORY_NO_DIFF_RETRY_LIMIT - 1},
        )
        exhausted = build_candidates(
            [],
            [target],
            no_diff_attempts_by_issue={31: FACTORY_NO_DIFF_RETRY_LIMIT},
        )
        self.assertTrue(any(item.kind == "pr" and item.number == 31 for item in below_budget))
        self.assertFalse(any(item.kind == "pr" and item.number == 31 for item in exhausted))

    def test_review_and_repair_prs_are_suppressed_at_retry_limit_without_relabeling(self) -> None:
        targets = [
            factory_pr(2122, stage="factory:changes-requested"),
            factory_pr(2132, stage="factory:review"),
        ]

        candidates = build_candidates(
            [],
            targets,
            no_diff_attempts_by_issue={
                2122: FACTORY_NO_DIFF_RETRY_LIMIT,
                2132: FACTORY_NO_DIFF_RETRY_LIMIT + 2,
            },
        )

        self.assertEqual(candidates, [])
        self.assertEqual(
            {name for target in targets for name in (label["name"] for label in target["labels"])},
            {"factory", "factory:unowned", "factory:changes-requested", "factory:review"},
        )

    def test_no_diff_pr_retry_suppression_expires_with_reset_window(self) -> None:
        now = 2_000_000_000
        expired = now - FACTORY_NO_DIFF_RETRY_RESET_SECONDS - 1
        comments = [
            {
                "author_association": "OWNER",
                "body": (
                    "<!-- comic-pile-factory-claim-released-v3:pr-31:worker:"
                    f"{expired}:repair-no-persisted-change-handoff -->"
                ),
            }
            for _ in range(FACTORY_NO_DIFF_RETRY_LIMIT)
        ]
        counts = no_diff_attempts_from_comments(comments, now_epoch=now)
        self.assertEqual(counts, {})
        candidates = build_candidates(
            [],
            [factory_pr(31, stage="factory:changes-requested")],
            no_diff_attempts_by_issue=counts,
        )
        self.assertTrue(any(item.kind == "pr" and item.number == 31 for item in candidates))

    def test_exhausted_pr_wakes_up_after_new_head(self) -> None:
        now = 2_000_000_000
        old_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        new_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        attempts = parse_no_diff_attempts_from_comments(
            [
                comment(pr_no_diff_marker(31, now - offset, sha=old_head))
                for offset in range(FACTORY_NO_DIFF_RETRY_LIMIT)
            ],
            now_epoch=now,
        )
        self.assertEqual(len(attempts), FACTORY_NO_DIFF_RETRY_LIMIT)
        still_same_head = build_candidates(
            [],
            [factory_pr(31, stage="factory:changes-requested", head=old_head)],
            no_diff_attempt_records=attempts,
        )
        after_push = build_candidates(
            [],
            [factory_pr(31, stage="factory:changes-requested", head=new_head)],
            no_diff_attempt_records=attempts,
        )
        self.assertFalse(any(item.kind == "pr" and item.number == 31 for item in still_same_head))
        self.assertTrue(any(item.kind == "pr" and item.number == 31 for item in after_push))

    def test_exhausted_pr_wakes_up_after_new_actionable_review_state(self) -> None:
        now = 2_000_000_000
        head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        attempts = parse_no_diff_attempts_from_comments(
            [
                comment(
                    pr_no_diff_marker(
                        31,
                        now - offset,
                        sha=head,
                        stage="factory:review",
                    )
                )
                for offset in range(FACTORY_NO_DIFF_RETRY_LIMIT)
            ],
            now_epoch=now,
        )
        still_review = build_candidates(
            [],
            [factory_pr(31, stage="factory:review", head=head)],
            no_diff_attempt_records=attempts,
        )
        after_changes = build_candidates(
            [],
            [factory_pr(31, stage="factory:changes-requested", head=head)],
            no_diff_attempt_records=attempts,
        )
        self.assertFalse(any(item.kind == "pr" and item.number == 31 for item in still_review))
        self.assertTrue(any(item.kind == "pr" and item.number == 31 for item in after_changes))

    def test_exhausted_pr_wakes_up_after_new_merge_conflict(self) -> None:
        now = 2_000_000_000
        head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        attempts = parse_no_diff_attempts_from_comments(
            [
                comment(pr_no_diff_marker(31, now - offset, sha=head, conflicted=False))
                for offset in range(FACTORY_NO_DIFF_RETRY_LIMIT)
            ],
            now_epoch=now,
        )
        still_clean = build_candidates(
            [],
            [factory_pr(31, stage="factory:changes-requested", head=head)],
            no_diff_attempt_records=attempts,
        )
        newly_conflicted = build_candidates(
            [],
            [
                factory_pr(
                    31,
                    stage="factory:changes-requested",
                    head=head,
                    mergeable="CONFLICTING",
                    merge_state="DIRTY",
                )
            ],
            no_diff_attempt_records=attempts,
        )
        self.assertFalse(any(item.kind == "pr" and item.number == 31 for item in still_clean))
        self.assertTrue(any(item.kind == "pr" and item.number == 31 for item in newly_conflicted))

    def test_legacy_unscoped_pr_markers_do_not_suppress_known_head(self) -> None:
        now = 2_000_000_000
        comments = [
            comment(
                "<!-- comic-pile-factory-claim-released-v3:pr-31:worker:"
                f"{now - offset}:repair-no-persisted-change-handoff -->"
            )
            for offset in range(FACTORY_NO_DIFF_RETRY_LIMIT)
        ]
        attempts = parse_no_diff_attempts_from_comments(comments, now_epoch=now)
        candidates = build_candidates(
            [],
            [factory_pr(31, stage="factory:changes-requested")],
            no_diff_attempt_records=attempts,
        )
        self.assertTrue(any(item.kind == "pr" and item.number == 31 for item in candidates))

    def test_explicitly_blocked_pr_remains_excluded(self) -> None:
        blocked = factory_pr(2140, stage="factory:blocked")

        self.assertEqual(build_candidates([], [blocked]), [])

    def test_real_factory_blocked_label_remains_terminal(self) -> None:
        candidates = build_candidates(
            [issue(31, "factory:blocked")],
            [],
            no_diff_attempts_by_issue={31: 0},
        )
        self.assertFalse(candidates)


class WipCapTests(unittest.TestCase):
    def ready_queue(self) -> list[dict[str, object]]:
        return [
            factory_pr(100 + offset, stage="factory:ready", worker=20 + offset)
            for offset in range(5)
        ]

    def active_pr_wip(self) -> list[dict[str, object]]:
        return [
            factory_pr(
                200 + offset,
                stage="factory:review",
                worker=20 + offset,
                owner=f"factory:{20 + offset}",
            )
            for offset in range(5)
        ]

    def test_ready_pr_queue_does_not_starve_ordinary_issue(self) -> None:
        candidates = build_candidates([issue(1)], self.ready_queue())
        self.assertTrue(any(item.kind == "issue" and item.number == 1 for item in candidates))

    def test_active_worker_pr_wip_preserves_backpressure(self) -> None:
        candidates = build_candidates([issue(1)], self.active_pr_wip())
        self.assertFalse(any(item.kind == "issue" and item.number == 1 for item in candidates))

    def test_wip_cap_allows_user_reported_bug(self) -> None:
        candidates = build_candidates(
            [issue(1, "user-reported", "bug")],
            self.active_pr_wip(),
        )
        self.assertTrue(any(item.kind == "issue" and item.number == 1 for item in candidates))

    def test_wip_cap_allows_critical_issue(self) -> None:
        candidates = build_candidates(
            [issue(1, "ralph-priority:critical")],
            self.active_pr_wip(),
        )
        self.assertTrue(any(item.kind == "issue" and item.number == 1 for item in candidates))

    def test_ready_pr_is_not_worker_candidate(self) -> None:
        candidates = build_candidates([], [factory_pr(1, stage="factory:ready")])
        self.assertFalse(any(item.kind == "pr" and item.number == 1 for item in candidates))

    def test_human_branch_with_factory_label_is_not_factory_candidate(self) -> None:
        candidates = build_candidates(
            [],
            [factory_pr(1, branch="chatgpt/human-authored-fix")],
        )
        self.assertFalse(candidates)

    def test_labeled_cursor_delivery_pr_is_factory_candidate(self) -> None:
        target = factory_pr(
            1,
            branch="cursor/issue-2184-queue-nested-scroll",
            owner="factory:unowned",
            stage="factory:review",
        )
        target["title"] = "Fix #2184: Queue nested scroll"
        candidates = build_candidates([], [target])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].linked_issue, 2184)
        self.assertTrue(candidates[0].kind == "pr")


class ReviewBacklogPressureTests(unittest.TestCase):
    def test_owned_review_prs_count_toward_backlog_saturation(self) -> None:
        from factory_work_policy import (
            FACTORY_REVIEW_BACKLOG_LIMIT,
            factory_review_backlog_count,
        )

        owned = [
            factory_pr(
                300 + offset,
                stage="factory:review",
                worker=20 + offset,
                owner=f"factory:{20 + offset}",
            )
            for offset in range(FACTORY_REVIEW_BACKLOG_LIMIT)
        ]
        self.assertEqual(factory_review_backlog_count(owned), FACTORY_REVIEW_BACKLOG_LIMIT)
        candidates = build_candidates([issue(1)], owned)
        self.assertFalse(any(item.kind == "issue" and item.number == 1 for item in candidates))

    def test_main_breakage_still_bypasses_saturated_review_backlog(self) -> None:
        from factory_work_policy import FACTORY_REVIEW_BACKLOG_LIMIT

        owned = [
            factory_pr(
                400 + offset,
                stage="factory:review",
                worker=30 + offset,
                owner=f"factory:{30 + offset}",
            )
            for offset in range(FACTORY_REVIEW_BACKLOG_LIMIT)
        ]
        candidates = build_candidates(
            [issue(1, "main-breakage", "bug", "user-reported")],
            owned,
        )
        self.assertTrue(any(item.kind == "issue" and item.number == 1 for item in candidates))


if __name__ == "__main__":
    unittest.main()
