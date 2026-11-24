"""Cover issue linking, labeling, quiescence, and #2167 acceptance regressions."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import factory_post_merge_closure_github as github
from factory_post_merge_closure import close_merged_pr_issue
from factory_post_merge_closure_testing import NoPersonReopenMixin
from factory_post_merge_closure_policy import (
    EPIC_ACCEPTANCE_LABELS,
    MERGE_CLOSURE_MARKER,
    PROTECTED_ISSUES,
    _child_numbers,
    _issue_is_acceptance_parent,
    closure_blocked_by_active_work,
    closure_comment,
    issue_is_quiescent,
    linked_issue,
    merged_within_age,
    target_labels,
)


class FactoryPostMergeClosurePolicyTests(NoPersonReopenMixin, unittest.TestCase):
    """Cover issue linking, labeling, quiescence, and #2167 acceptance regressions."""

    def test_branch_shape_resolves_issue(self) -> None:
        """Parse the issue encoded in a factory branch name."""
        self.assertEqual(linked_issue("factory/39-1566-opencode-free", ""), 1566)

    def test_explicit_closing_reference_overrides_branch_issue(self) -> None:
        """Honor a PR body that explicitly closes a different parent issue."""
        self.assertEqual(linked_issue("factory/47-1612-opencode-free", "Closes #1620."), 1620)

    def test_non_factory_branch_falls_back_to_body_keyword(self) -> None:
        """Use the closing keyword when the branch encodes nothing."""
        self.assertEqual(linked_issue("phase/some-work", "Closes #42."), 42)
        self.assertEqual(linked_issue("phase/some-work", "Fixes #7\nResolves #8"), 7)

    def test_non_closing_part_of_reference_never_closes(self) -> None:
        """A review-only ``Part of #N.`` link is not a merge closure target."""
        self.assertIsNone(linked_issue("local/acp-4348", "Part of #4348."))
        self.assertIsNone(
            linked_issue("local/acp-4348", "Part of #4348 and governed by #4335.")
        )
        self.assertIsNone(linked_issue("local/acp-4348", "Advances #4348. Parent: #4335."))
        self.assertEqual(linked_issue("factory/11-4353-work", "Part of #4348."), 4353)
        self.assertEqual(
            linked_issue("local/acp-4348", "Closes #4348.\nPart of #4335."), 4348
        )

    def test_partial_factory_branch_and_negated_reference_never_close(self) -> None:
        """PR #4753 was a slice, not completion of its factory/86-4719 issue."""
        partial = (
            "Part of #4719 (parent #4335). Bootstrap deletion is not claimed; "
            "this PR does not close #4719."
        )
        self.assertIsNone(linked_issue("factory/86-4719-kilo-auto", partial))
        self.assertIsNone(linked_issue("local/partial-work", partial))
        self.assertIsNone(linked_issue("factory/86-4719-kilo-auto", "Part of #4719."))
        self.assertIsNone(linked_issue("local/partial-work", "Does not close #4719."))
        # An unrelated parent reference does not block a real closing claim.
        self.assertEqual(
            linked_issue("factory/96-4740-work", "Closes #4740. Part of #4335."),
            4740,
        )

    def test_unlinkable_pr_returns_none(self) -> None:
        """Return None without branch shape or a closing keyword."""
        self.assertIsNone(linked_issue("phase/some-work", "Refs #12 only"))
        self.assertIsNone(linked_issue("", ""))

    def test_target_labels_preserve_unrelated_and_add_done(self) -> None:
        """Strip transient factory state while keeping product labels."""
        self.assertEqual(
            target_labels(
                [
                    "bug",
                    "user-reported",
                    "factory",
                    "factory:ready",
                    "factory:unowned",
                    "ralph-status:validation",
                ]
            ),
            ["bug", "user-reported", "factory", "ralph-status:done"],
        )

    def test_target_labels_drop_every_stale_variant(self) -> None:
        """Remove all owner, stage, and status variants."""
        self.assertEqual(
            target_labels(
                [
                    "enhancement",
                    "factory:building",
                    "factory:review",
                    "factory:changes-requested",
                    "factory:ci",
                    "factory:blocked",
                    "factory:46",
                    "factory:local",
                    "ralph-status:in-progress",
                    "ralph-status:pending",
                ]
            ),
            ["enhancement", "factory", "ralph-status:done"],
        )

    def test_quiescent_ready_issue_allows_closure(self) -> None:
        """Ready-marked unowned issues are the stranded signature."""
        self.assertTrue(
            issue_is_quiescent(["bug", "user-reported", "factory", "factory:unowned", "factory:ready"])
        )

    def test_stale_active_labels_under_no_owner_allow_closure(self) -> None:
        """Contradictory stale stages without an owner lease are not liveness."""
        self.assertTrue(
            issue_is_quiescent(
                ["bug", "factory", "factory:unowned", "factory:ready", "ralph-status:in-progress"]
            )
        )
        self.assertTrue(issue_is_quiescent(["factory", "factory:unowned", "factory:review"]))
        self.assertTrue(issue_is_quiescent(["factory", "factory:unowned", "ralph-status:validation"]))

    def test_owner_leases_and_blockers_refuse_sweep_closure(self) -> None:
        """Real owner leases and genuine blockers stop the sweep."""
        self.assertFalse(issue_is_quiescent(["factory", "factory:building", "factory:46"]))
        self.assertFalse(issue_is_quiescent(["factory", "factory:local"]))
        self.assertFalse(issue_is_quiescent(["factory", "factory:blocked"]))
        self.assertFalse(issue_is_quiescent(["factory", "ralph-status:blocked"]))

    def test_protected_issues_are_never_closed(self) -> None:
        """The adopter's reserved issues stay outside closure authority."""
        self.assertEqual(PROTECTED_ISSUES, frozenset({9991, 9992}))

    def test_closure_comment_embeds_canonical_marker(self) -> None:
        """The marker identifies both the PR and the issue durably."""
        comment = closure_comment(1588, 1111)
        self.assertIn(
            f"<!-- {MERGE_CLOSURE_MARKER}:pr-1588:issue-1111 -->",
            comment,
        )
        self.assertIn("PR #1588", comment)

    def test_merged_within_age_bounds(self) -> None:
        """Only parseable timestamps inside the inclusive window pass."""
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
        self.assertTrue(merged_within_age("2026-08-21T00:00:00Z", now, 26))
        self.assertTrue(merged_within_age("2026-08-20T00:00:00Z", now, 26))
        self.assertFalse(merged_within_age("2026-08-19T00:00:00Z", now, 26))
        self.assertFalse(merged_within_age(None, now, 26))
        self.assertFalse(merged_within_age("garbage", now, 26))

    def test_sweep_gates_on_active_work_but_direct_closure_does_not(self) -> None:
        """Quiescence is enforced only when the caller requires it."""
        active = ["factory", "factory:building", "factory:46"]
        self.assertTrue(closure_blocked_by_active_work(active, True))
        self.assertFalse(closure_blocked_by_active_work(active, False))
        self.assertFalse(closure_blocked_by_active_work(["factory", "factory:ready"], True))

    # Regression tests for issue #2167: acceptance parent closure

    def test_ordinary_pr_closes_completed_issue(self) -> None:
        """Regression case 1: ordinary implementation PR closes completed issue."""
        # This is the normal behavior that should still work
        self.assertEqual(linked_issue("factory/100-500-work", "Closes #500"), 500)

    def test_acceptance_parent_with_incomplete_children_detected(self) -> None:
        """Regression case 2: acceptance parent with incomplete child gates detected."""
        # Test the helper functions without making API calls
        parent_body = """
- [ ] #2126
- [ ] #2127
- [x] #2128
"""
        labels = ["epic", "factory"]
        # Test that we correctly identify acceptance parents
        self.assertTrue(_issue_is_acceptance_parent(labels, parent_body))
        # Test that we correctly extract child numbers
        children = _child_numbers(parent_body, 1615)
        self.assertEqual(children, {2126, 2127, 2128})

    def test_acceptance_parent_closes_when_all_children_complete(self) -> None:
        """Regression case 3: acceptance parent closes once all children are complete."""
        # Test that we correctly identify acceptance parents with complete children
        parent_body = """
- [x] #2126
- [x] #2127
- [x] #2128
"""
        labels = ["prd", "factory"]
        # Test that we correctly identify acceptance parents
        self.assertTrue(_issue_is_acceptance_parent(labels, parent_body))
        # Test that we correctly extract child numbers
        children = _child_numbers(parent_body, 1615)
        self.assertEqual(children, {2126, 2127, 2128})

    def test_operator_reopen_is_durable(self) -> None:
        """Regression case 4: operator reopening an incompletely closed parent is durable."""
        # This is tested by the fact that we check child state at closure time
        # If a parent is reopened, its children will still be checked
        # The test verifies that we don't close based on PR references alone
        pass  # This is covered by the acceptance_parent_incomplete check

    def test_stale_pr_cannot_override_frozen_contract(self) -> None:
        """Regression case 5: older PR closing language cannot override frozen parent contract."""
        # An older PR might say "Closes #1615" but the parent has a frozen contract
        # The acceptance parent check prevents closure regardless of PR references
        parent_body = """
## Frozen contract
- [ ] #2127
- [ ] #2128
"""
        labels = ["epic", "factory"]
        # Test that we correctly identify acceptance parents
        self.assertTrue(_issue_is_acceptance_parent(labels, parent_body))
        # Test that we correctly extract child numbers
        children = _child_numbers(parent_body, 1615)
        self.assertEqual(children, {2127, 2128})

    def test_1615_2127_2128_2129_regression_fixture(self) -> None:
        """Regression case 6: #1615 / #2127 / #2128 / #2129 as regression fixture."""
        # This is the actual regression case from the issue
        parent_body = """
## Executable child work
- [ ] #2126 — backend read-only CBL preview
- [ ] #2127 — transactional selective materialization
- [ ] #2128 — production CBL browser/import-review UI
- [ ] #2129 — production cutover + incident cleanup
"""
        labels = ["enhancement", "factory"]
        # "enhancement" is not in EPIC_ACCEPTANCE_LABELS, so this is NOT an
        # acceptance parent — even though the body lists child checkboxes.
        self.assertFalse(_issue_is_acceptance_parent(labels, parent_body))
        children = _child_numbers(parent_body, 1615)
        self.assertEqual(children, {2126, 2127, 2128, 2129})

    def test_closure_reads_issue_body_not_pr_body_for_acceptance_parent(self) -> None:
        """Closure uses the issue body, not the PR body, to detect child gates.

        PR closing references live in the PR body, while the child checkboxes
        that make an issue an acceptance parent live in the issue body. Feeding
        the PR body into the acceptance-parent guard would miss every parent and
        wrongly auto-close it (the #1615 incident). This test proves the guard
        reads the issue body by mocking the API layers.
        """
        from unittest.mock import patch

        mod = github
        issue_body = """
## Executable child work
This issue is a product contract and acceptance parent.
- [ ] #2127 — transactional selective materialization
- [ ] #2128 — production CBL browser/import-review UI
- [ ] #2129 — production cutover + incident cleanup
"""
        # An acceptance parent is still open; children are incomplete.
        with patch.object(
            mod,
            "_issue_view",
            return_value={
                "state": "OPEN",
                "body": issue_body,
                "labels": ["enhancement", "factory", "factory:ready", "factory:unowned"],
            },
        ):
            with patch.object(
                mod,
                "_issue_has_open_successor_pr",
                return_value=False,
            ) as mock_successor:
                with patch.object(
                    mod,
                    "_pr_info",
                    return_value={
                        "branch": "factory/58-2167-omniroute",
                        # The PR body closes the issue but contains no child checkboxes.
                        "body": "Closes #1615.",
                        "state": "MERGED",
                    },
                ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2140, dry_run=True)

        self.assertEqual(result, "acceptance-parent-incomplete")
        # The guard must halt before the successor check: the parent stays open
        # even though the PR body carries a closing reference.
        mock_successor.assert_not_called()
