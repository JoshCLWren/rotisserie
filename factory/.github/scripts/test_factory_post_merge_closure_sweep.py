"""Cover sweep dependency handling and keyword-closed issue repair (#4083)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

import factory_implementation_delivery as implementation_delivery
import factory_post_merge_closure_github as github
from factory_post_merge_closure import close_merged_pr_issue, sweep_stranded_closures
from factory_post_merge_closure_testing import ClosureIssueMixin


class FactoryPostMergeClosureSweepTests(ClosureIssueMixin, unittest.TestCase):
    """Cover sweep dependency handling and keyword-closed issue repair (#4083)."""

    def test_sweep_reports_open_dependency_and_never_closes(self) -> None:
        """The stranded closure sweep honors the same prerequisite gate."""
        from unittest.mock import patch

        mod = github
        merged_at = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = json.dumps(
            [
                {
                    "number": 4024,
                    "headRefName": "factory/33-4010-kilo-auto",
                    "body": "Closes #4010.",
                    "mergedAt": merged_at,
                }
            ]
        )
        with patch.object(mod, "_repo_slug", return_value="owner/repo"):
            with patch.object(mod, "_run_gh", return_value=listing):
                with patch.object(mod, "_pr_info", return_value={
                    "branch": "factory/33-4010-kilo-auto",
                    "body": "Closes #4010.",
                    "state": "MERGED",
                }):
                    with patch.object(
                        mod,
                        "_issue_view",
                        side_effect=[
                            self._closure_issue(4010, body="Depends on #4008"),
                            self._closure_issue(4008, state="OPEN"),
                        ],
                    ):
                        with patch.object(
                            mod, "_issue_has_open_successor_pr", return_value=False
                        ):
                                with patch.object(
                                    mod, "_native_open_blockers", return_value=[]
                                ):
                                    with patch.object(
                                        implementation_delivery, "complete_pr_diff",
                                        return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                                      "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                                    ):
                                        with patch.object(mod, "_close_issue") as mock_close:
                                            outcomes = sweep_stranded_closures(dry_run=True)
        self.assertEqual(outcomes, ["PR 4024: open-dependency: #4008"])
        mock_close.assert_not_called()

    def test_sweep_closes_when_dependencies_complete(self) -> None:
        """The sweep proceeds normally once every prerequisite closed."""
        from unittest.mock import patch

        mod = github
        merged_at = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = json.dumps(
            [
                {
                    "number": 4034,
                    "headRefName": "factory/41-4017-kilo-auto",
                    "body": "Closes #4017.",
                    "mergedAt": merged_at,
                }
            ]
        )
        with patch.object(mod, "_repo_slug", return_value="owner/repo"):
            with patch.object(mod, "_run_gh", return_value=listing):
                with patch.object(mod, "_pr_info", return_value={
                    "branch": "factory/41-4017-kilo-auto",
                    "body": "Closes #4017.",
                    "state": "MERGED",
                }):
                    with patch.object(
                        mod,
                        "_issue_view",
                        side_effect=[
                            self._closure_issue(4017, body="Depends on #4016"),
                            self._closure_issue(4016, state="CLOSED"),
                        ],
                    ):
                        with patch.object(
                            mod, "_issue_has_open_successor_pr", return_value=False
                        ):
                            with patch.object(
                                mod, "_native_open_blockers", return_value=[]
                            ):
                                with patch.object(
                                    implementation_delivery, "complete_pr_diff",
                                    return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                                  "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                                ):
                                    outcomes = sweep_stranded_closures(dry_run=True)
        self.assertEqual(
            outcomes,
            ["PR 4034: dry-run:replace-labels-with"
             "['enhancement', 'factory', 'ralph-status:done']+close"],
        )

    def test_open_declared_dependency_blocks_closure_4017_4016(self) -> None:
        """CBL regression #4017/#4016: an open prerequisite refuses closure."""
        from unittest.mock import patch

        mod = github
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(
                mod,
                "_issue_view",
                side_effect=[
                    self._closure_issue(4017, body="Tighten the docstring contract.\nDepends on #4016"),
                    self._closure_issue(4016, state="OPEN"),
                ],
            ) as mock_view:
                with patch.object(
                    mod,
                    "_pr_info",
                    return_value={
                        "branch": "factory/41-4017-kilo-auto",
                        "body": "Closes #4017.",
                        "state": "MERGED",
                    },
                ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(
                            mod, "_issue_has_open_successor_pr", return_value=False
                        ):
                            with patch.object(
                                implementation_delivery, "complete_pr_diff",
                                return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                              "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                            ):
                                result = close_merged_pr_issue(4034, dry_run=True)
        self.assertEqual(result, "open-dependency: #4016")
        numbers = [call.args[0] for call in mock_view.call_args_list]
        self.assertEqual(numbers, [4017, 4016])
        mock_close.assert_not_called()

    # Regression tests for issue #4083: keyword-closed issue with dependencies (G1-G4)

    def test_keyword_closed_issue_open_deps_reopens_blocked_unowned(self) -> None:
        """Keyword-closed issue with open deps reopens with blocked+unowned labels (G1, G2)."""
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
                            "body": "Delivered work.\nDepends on #4008",
                            "labels": [
                                "enhancement",
                                "factory",
                                "factory:blocked",
                                "ralph-status:blocked",
                                "factory:unowned",
                            ],
                        },
                        self._closure_issue(4008, state="OPEN"),
                    ],
                ) as mock_view:
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/33-4010-kilo-auto",
                            "body": "Closes #4010.",
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
                                                            return_value={4008},
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4024, dry_run=False
                                                            )
        self.assertEqual(result, "open-dependency: #4008")
        numbers = [call.args[0] for call in mock_view.call_args_list]
        self.assertEqual(numbers, [4010, 4008])
        mock_close.assert_not_called()
        replaced = mock_replace.call_args[0][1]
        self.assertIn("factory:blocked", replaced)
        self.assertIn("ralph-status:blocked", replaced)
        self.assertIn("factory:unowned", replaced)

    def test_keyword_closed_issue_unreadable_deps_reopens_pending_unowned(self) -> None:
        """Keyword-closed issue with unreadable deps reopens with pending+unowned (G1, A10)."""
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
                        "labels": ["enhancement", "factory", "factory:blocked", "ralph-status:blocked", "factory:unowned"],
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
                                mod, "_has_dependency_block_marker", return_value=False
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
                                                        with patch.object(
                                                            mod.eligibility,
                                                            "parse_declared_dependencies",
                                                            return_value={4024},
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4041, dry_run=False
                                                            )
        self.assertEqual(result, "open-dependency")
        mock_close.assert_not_called()
        replaced = mock_replace.call_args[0][1]
        self.assertIn("ralph-status:pending", replaced)
        self.assertIn("factory:unowned", replaced)
        self.assertNotIn("factory:blocked", replaced)
        self.assertNotIn("ralph-status:blocked", replaced)
