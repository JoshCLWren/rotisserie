"""Cover closure dedupe, marker reads, and reopened blocked issues."""

from __future__ import annotations

import unittest

import factory_implementation_delivery as implementation_delivery
import factory_post_merge_closure_github as github
from factory_post_merge_closure_github import (
    _has_closure_marker,
    _has_dependency_block_marker,
    _has_unreadable_dependency_warning,
)
from factory_post_merge_closure import close_merged_pr_issue
from factory_post_merge_closure_policy import MERGE_CLOSURE_MARKER
from factory_post_merge_closure_testing import ClosureIssueMixin


class FactoryPostMergeClosureReopenTests(ClosureIssueMixin, unittest.TestCase):
    """Cover closure dedupe, marker reads, and reopened blocked issues."""

    def test_keyword_closed_issue_deps_resolved_closes_without_duplicate_comment(self) -> None:
        """Keyword-closed issue whose deps finished closes without repeating the comment (G1, G3)."""
        from unittest.mock import patch

        mod = github
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    side_effect=[
                        {
                            "state": "CLOSED",
                            "body": "Delivered work.\nDepends on #4016",
                            "labels": [
                                "enhancement",
                                "factory",
                                "factory:blocked",
                                "ralph-status:blocked",
                                "factory:unowned",
                            ],
                        },
                        self._closure_issue(4016, state="CLOSED"),
                    ],
                ) as mock_view:
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(
                                    mod, "_has_closure_marker", return_value=False
                                ):
                                    with patch.object(mod, "_run_gh", return_value="[]"):
                                        with patch.object(
                                            mod,
                                            "_comment_issue",
                                            side_effect=lambda _issue, body: comments.append(body),
                                        ):
                                            with patch.object(
                                                mod, "_issue_has_open_successor_pr",
                                                return_value=False,
                                            ):
                                                with patch.object(
                                                    mod, "_native_open_blockers", return_value=[]
                                                ):
                                                    with patch.object(
                                                        implementation_delivery,
                                                        "complete_pr_diff",
                                                        return_value=(
                                                            ["app/fix.py"],
                                                            "diff --git a/app/fix.py b/app/fix.py\n"
                                                            "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                        ),
                                                    ):
                                                        with patch.object(
                                                            mod.eligibility,
                                                            "parse_declared_dependencies",
                                                            return_value={4016},
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4034, dry_run=False
                                                            )
        self.assertEqual(result, "closed")
        numbers = [call.args[0] for call in mock_view.call_args_list]
        self.assertEqual(numbers, [4017, 4016])
        mock_close.assert_not_called()
        replaced = mock_replace.call_args[0][1]
        self.assertNotIn("factory:blocked", replaced)
        self.assertNotIn("ralph-status:blocked", replaced)
        self.assertIn("factory", replaced)
        self.assertEqual(len(comments), 1)
        self.assertIn(f"{MERGE_CLOSURE_MARKER}:pr-4034:issue-4017", comments[0])

    def test_repeated_sweep_does_not_repeat_closure_comment(self) -> None:
        """A sweep that re-visits an already-closed issue does not repost the closure comment (G3)."""
        from unittest.mock import patch

        mod = github
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "CLOSED",
                        "body": "Delivered work.",
                        "labels": ["enhancement", "factory", "ralph-status:done"],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=False
                            ):
                                with patch.object(
                                    mod, "_has_closure_marker", return_value=True
                                ):
                                    with patch.object(mod, "_run_gh", return_value="[]"):
                                        with patch.object(
                                            mod,
                                            "_comment_issue",
                                            side_effect=lambda _issue, body: comments.append(body),
                                        ):
                                            with patch.object(
                                                mod, "_issue_has_open_successor_pr",
                                                return_value=False,
                                            ):
                                                with patch.object(
                                                    mod, "_native_open_blockers", return_value=[]
                                                ):
                                                    with patch.object(
                                                        implementation_delivery,
                                                        "complete_pr_diff",
                                                        return_value=(
                                                            ["app/fix.py"],
                                                            "diff --git a/app/fix.py b/app/fix.py\n"
                                                            "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                        ),
                                                    ):
                                                        with patch.object(
                                                            mod.eligibility,
                                                            "parse_declared_dependencies",
                                                            return_value=set(),
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4034, dry_run=False
                                                            )
        self.assertEqual(result, "closed")
        mock_close.assert_not_called()
        # The marker only skips the comment; the canonical label set is still
        # restored on this repeated visit (G3).
        replaced = mock_replace.call_args[0][1]
        self.assertIn("ralph-status:done", replaced)
        self.assertNotIn("factory:blocked", replaced)
        self.assertNotIn("ralph-status:blocked", replaced)
        self.assertEqual(comments, [])

    def test_marker_helper_treats_none_payload_as_absent(self) -> None:
        """A None gh payload reads as no marker instead of raising (CI regression)."""
        from unittest.mock import patch

        mod = github
        with patch.object(mod, "_run_gh", return_value=None):
            self.assertFalse(_has_closure_marker(4001, 5001))
            self.assertFalse(_has_dependency_block_marker(4001, 5001))
            self.assertFalse(_has_unreadable_dependency_warning(4001, 5001))

    def test_reopened_blocked_issue_keeps_live_owner_when_deps_finish(self) -> None:
        """A claimed reopened issue is never relabeled or closed by the sweep (G2)."""
        from unittest.mock import patch

        mod = github
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "OPEN",
                        "body": "Delivered work.\nDepends on #4016",
                        "labels": [
                            "enhancement",
                            "factory",
                            "factory:blocked",
                            "ralph-status:blocked",
                            "factory:7",
                        ],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=False):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(mod, "_has_closure_marker", return_value=False):
                                    with patch.object(
                                        mod, "_has_unreadable_dependency_warning",
                                        return_value=False,
                                    ):
                                        with patch.object(
                                            mod, "_native_open_blockers", return_value=[]
                                        ):
                                            with patch.object(
                                                mod, "_run_gh", return_value="[]"
                                            ):
                                                with patch.object(
                                                    implementation_delivery,
                                                    "complete_pr_diff",
                                                    return_value=(
                                                        ["app/fix.py"],
                                                        "diff --git a/app/fix.py b/app/fix.py\n"
                                                        "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                    ),
                                                ):
                                                    with patch.object(
                                                        mod.eligibility,
                                                        "parse_declared_dependencies",
                                                        return_value={4016},
                                                    ):
                                                        result = close_merged_pr_issue(
                                                            4034, dry_run=False
                                                        )
        self.assertEqual(result, "active-work")
        mock_close.assert_not_called()
        mock_replace.assert_not_called()

    def test_reopened_blocked_issue_keeps_open_successor_when_deps_finish(self) -> None:
        """An open successor PR protects the reopened issue from closure (G2)."""
        from unittest.mock import patch

        mod = github
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "OPEN",
                        "body": "Delivered work.\nDepends on #4016",
                        "labels": [
                            "enhancement",
                            "factory",
                            "factory:blocked",
                            "ralph-status:blocked",
                            "factory:unowned",
                        ],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=False):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(
                                    mod, "_issue_has_open_successor_pr", return_value=True
                                ):
                                    with patch.object(
                                        mod, "_native_open_blockers", return_value=[]
                                    ):
                                        with patch.object(
                                            mod, "_run_gh", return_value="[]"
                                        ):
                                            with patch.object(
                                                implementation_delivery,
                                                "complete_pr_diff",
                                                return_value=(
                                                    ["app/fix.py"],
                                                    "diff --git a/app/fix.py b/app/fix.py\n"
                                                    "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                ),
                                            ):
                                                with patch.object(
                                                    mod.eligibility,
                                                    "parse_declared_dependencies",
                                                    return_value={4016},
                                                ):
                                                    result = close_merged_pr_issue(
                                                        4034, dry_run=False
                                                    )
        self.assertEqual(result, "successor-open")
        mock_close.assert_not_called()
        mock_replace.assert_not_called()

    def test_reclosed_issue_still_restores_blocked_labels_with_old_marker(self) -> None:
        """A second completion after reopen repairs labels despite the old closure marker (G3)."""
        from unittest.mock import patch

        mod = github
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "CLOSED",
                        "body": "Delivered work.\nDepends on #4016",
                        "labels": [
                            "enhancement",
                            "factory",
                            "factory:blocked",
                            "ralph-status:blocked",
                            "factory:unowned",
                        ],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(
                                    mod, "_has_closure_marker", return_value=True
                                ):
                                    with patch.object(mod, "_run_gh", return_value="[]"):
                                        with patch.object(
                                            mod,
                                            "_comment_issue",
                                            side_effect=lambda _issue, body: comments.append(body),
                                        ):
                                            with patch.object(
                                                mod, "_issue_has_open_successor_pr",
                                                return_value=False,
                                            ):
                                                with patch.object(
                                                    mod, "_native_open_blockers", return_value=[]
                                                ):
                                                    with patch.object(
                                                        implementation_delivery,
                                                        "complete_pr_diff",
                                                        return_value=(
                                                            ["app/fix.py"],
                                                            "diff --git a/app/fix.py b/app/fix.py\n"
                                                            "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                        ),
                                                    ):
                                                        with patch.object(
                                                            mod.eligibility,
                                                            "parse_declared_dependencies",
                                                            return_value={4016},
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4034, dry_run=False
                                                            )
        self.assertEqual(result, "closed")
        mock_close.assert_not_called()
        self.assertEqual(comments, [])
        replaced = mock_replace.call_args[0][1]
        self.assertNotIn("factory:blocked", replaced)
        self.assertNotIn("ralph-status:blocked", replaced)
        self.assertNotIn("factory:unowned", replaced)
        self.assertIn("ralph-status:done", replaced)

    def test_unreadable_dependency_warning_is_deduplicated(self) -> None:
        """A past unreadable-dependency warning never reposts on later sweeps (G4)."""
        from unittest.mock import patch

        mod = github
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "CLOSED",
                        "body": "Delivered work.\nDepends on #4024",
                        "labels": [
                            "enhancement",
                            "factory",
                            "factory:blocked",
                            "ralph-status:blocked",
                            "factory:unowned",
                        ],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/33-4025-kilo-auto",
                            "body": "Closes #4025.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(mod, "_has_closure_marker", return_value=False):
                                    with patch.object(
                                        mod, "_has_unreadable_dependency_warning", return_value=True
                                    ):
                                        with patch.object(mod, "_run_gh", return_value="[]"):
                                            with patch.object(
                                                mod,
                                                "_comment_issue",
                                                side_effect=lambda _issue, body: comments.append(body),
                                            ):
                                                with patch.object(
                                                    mod, "_issue_has_open_successor_pr",
                                                    return_value=False,
                                                ):
                                                    with patch.object(
                                                        mod, "_native_open_blockers",
                                                        side_effect=RuntimeError("gh failed"),
                                                    ):
                                                        with patch.object(
                                                            implementation_delivery,
                                                            "complete_pr_diff",
                                                            return_value=(
                                                                ["app/fix.py"],
                                                                "diff --git a/app/fix.py b/app/fix.py\n"
                                                                "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                            ),
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4041, dry_run=False
                                                            )
        self.assertEqual(result, "open-dependency")
        mock_close.assert_not_called()
        self.assertEqual(comments, [])
        replaced = mock_replace.call_args[0][1]
        self.assertIn("ralph-status:pending", replaced)
        self.assertIn("factory:unowned", replaced)
