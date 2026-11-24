"""Cover the declared and native dependency closure gate (#4041)."""

from __future__ import annotations

import json
import unittest

import factory_implementation_delivery as implementation_delivery
import factory_post_merge_closure_github as github
from factory_post_merge_closure_github import dependency_block_marker
from factory_post_merge_closure import close_merged_pr_issue
from factory_post_merge_closure_testing import ClosureIssueMixin


class FactoryPostMergeClosureDependencyTests(ClosureIssueMixin, unittest.TestCase):
    """Cover the declared and native dependency closure gate (#4041)."""

    # Declared-dependency closure gate (issue #4041): a merged PR must never
    # mark a dependent issue done while its prerequisite is still open.

    def test_open_declared_dependency_blocks_closure(self) -> None:
        """CBL regression #4010/#4008: an open prerequisite refuses closure."""
        from unittest.mock import patch

        mod = github
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(
                mod,
                "_issue_view",
                side_effect=[
                    self._closure_issue(4010, body="Resolve the duplicate snapshot bug.\nDepends on #4008"),
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
                                result = close_merged_pr_issue(4024, dry_run=True)
        self.assertEqual(result, "open-dependency: #4008")
        numbers = [call.args[0] for call in mock_view.call_args_list]
        self.assertEqual(numbers, [4010, 4008])
        mock_close.assert_not_called()

    def test_blocked_closure_records_durable_dependency_reason(self) -> None:
        """A blocked closure leaves one durable reason naming the blockers."""
        from unittest.mock import patch

        mod = github
        comments: list[str] = []
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4010, body="Depends on #4008"),
                self._closure_issue(4008, state="OPEN"),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4010-kilo-auto",
                    "body": "Closes #4010.",
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
                            with patch.object(mod, "_run_gh", return_value="[]"):
                                with patch.object(
                                    mod,
                                    "_comment_issue",
                                    side_effect=lambda _issue, body: comments.append(body),
                                ):
                                    with patch.object(mod, "_close_issue") as mock_close:
                                        result = close_merged_pr_issue(4024)
        self.assertEqual(result, "open-dependency: #4008")
        mock_close.assert_not_called()
        self.assertEqual(len(comments), 1)
        self.assertIn(
            "widgets-factory-dependency-block-v1:pr-4024:issue-4010:deps-4008",
            comments[0],
        )
        self.assertIn("#4008", comments[0])

    def test_blocked_closure_does_not_repeat_dependency_reason(self) -> None:
        """The same blocker set never produces a second reason comment."""
        from unittest.mock import patch

        mod = github
        marker = dependency_block_marker(4024, 4010, [4008])
        existing = json.dumps([[{"body": f"{marker}\n\nAlready explained."}]])
        comments: list[str] = []
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4010, body="Depends on #4008"),
                self._closure_issue(4008, state="OPEN"),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4010-kilo-auto",
                    "body": "Closes #4010.",
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
                            with patch.object(mod, "_run_gh", return_value=existing):
                                with patch.object(
                                    mod, "_replace_labels"
                                ) as mock_replace:
                                    with patch.object(
                                        mod,
                                        "_comment_issue",
                                        side_effect=lambda _issue, body: comments.append(body),
                                    ):
                                        result = close_merged_pr_issue(4024)
        self.assertEqual(result, "open-dependency: #4008")
        self.assertEqual(comments, [])
        # The label write is real in production but must never escape the
        # self-test: it previously performed an unauthenticated gh call and
        # made every merge-drain run red (#4464).
        mock_replace.assert_called_once()

    def test_closed_declared_dependency_allows_closure(self) -> None:
        """CBL regression #4017/#4016: a closed prerequisite does not block."""
        from unittest.mock import patch

        mod = github
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4017, body="Tighten the docstring contract.\nDepends on #4016"),
                self._closure_issue(4016, state="CLOSED"),
            ],
        ):
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
        # The dependency check passes and the closure flow reaches its
        # labeling/close decision instead of being refused outright. The
        # successor lookup is mocked so this self-test stays hermetic in the
        # no-GH_TOKEN merge-drain self-test step.
        self.assertEqual(
            result,
            "dry-run:replace-labels-with"
            "['enhancement', 'factory', 'ralph-status:done']+close",
        )

    def test_reopened_dependency_blocks_closure_again(self) -> None:
        """A prerequisite reopened after delivery still refuses fresh closure."""
        from unittest.mock import patch

        mod = github
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(
                mod,
                "_issue_view",
                side_effect=[
                    self._closure_issue(4030, body="Depends on #4010"),
                    self._closure_issue(4010, state="OPEN"),
                ],
            ):
                with patch.object(
                    mod,
                    "_pr_info",
                    return_value={
                        "branch": "factory/33-4030-kilo-auto",
                        "body": "Closes #4030.",
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
                                result = close_merged_pr_issue(4040, dry_run=True)
        self.assertEqual(result, "open-dependency: #4010")
        mock_close.assert_not_called()

    def test_failed_dependency_lookup_fails_closed(self) -> None:
        """An unreadable prerequisite state blocks closure instead of closing."""
        from unittest.mock import patch

        mod = github
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4025, body="Depends on #4024"),
                RuntimeError("gh failed"),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4025-kilo-auto",
                    "body": "Closes #4025.",
                    "state": "MERGED",
                },
            ):
                with patch.object(
                    mod, "_issue_has_open_successor_pr", return_value=False
                ):
                    with patch.object(
                        implementation_delivery, "complete_pr_diff",
                        return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                      "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                    ):
                        result = close_merged_pr_issue(4041, dry_run=True)
        self.assertEqual(result, "open-dependency")

    def test_open_native_blocked_by_blocks_closure(self) -> None:
        """GitHub's native blocked-by links block like declared text."""
        from unittest.mock import patch

        mod = github
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4050, body="No text dependencies declared."),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4050-kilo-auto",
                    "body": "Closes #4050.",
                    "state": "MERGED",
                },
            ):
                with patch.object(mod, "_native_open_blockers", return_value=[4049]):
                    with patch.object(
                        mod, "_issue_has_open_successor_pr", return_value=False
                    ):
                        with patch.object(
                            implementation_delivery, "complete_pr_diff",
                            return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                          "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                        ):
                            result = close_merged_pr_issue(4051, dry_run=True)
        self.assertEqual(result, "open-dependency: #4049")

    def test_closed_native_blocker_allows_closure(self) -> None:
        """A natively recorded blocker already closed does not block."""
        from unittest.mock import patch

        mod = github
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4050, body="No text dependencies declared."),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4050-kilo-auto",
                    "body": "Closes #4050.",
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
                            result = close_merged_pr_issue(4051, dry_run=True)
        # The successor lookup is mocked so this self-test stays hermetic in
        # the no-GH_TOKEN merge-drain self-test step.
        self.assertEqual(
            result,
            "dry-run:replace-labels-with"
            "['enhancement', 'factory', 'ralph-status:done']+close",
        )

    def test_failed_native_blocker_lookup_fails_closed(self) -> None:
        """An unreadable native blocked-by graph refuses closure."""
        from unittest.mock import patch

        mod = github
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4050, body="No text dependencies declared."),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4050-kilo-auto",
                    "body": "Closes #4050.",
                    "state": "MERGED",
                },
            ):
                with patch.object(
                    mod, "_native_open_blockers", side_effect=RuntimeError("gh failed")
                ):
                    with patch.object(
                        mod, "_issue_has_open_successor_pr", return_value=False
                    ):
                        with patch.object(
                            implementation_delivery, "complete_pr_diff",
                            return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                          "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                        ):
                            result = close_merged_pr_issue(4051, dry_run=True)
        self.assertEqual(result, "open-dependency")

    def test_conflicting_branch_and_closing_reference_checks_the_delivered_issue(self) -> None:
        """A branch naming #1612 with ``Closes #1620`` checks #1620's state."""
        from unittest.mock import patch

        mod = github
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(1620, body="Delivered work.\nDepends on #4008"),
                self._closure_issue(4008, state="OPEN"),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/47-1612-opencode-free",
                    "body": "Closes #1620.",
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
                            result = close_merged_pr_issue(4050, dry_run=True)
        self.assertEqual(result, "open-dependency: #4008")
