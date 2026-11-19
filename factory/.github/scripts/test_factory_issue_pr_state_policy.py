#!/usr/bin/env python3
"""Regression tests for trusted factory issue-state precedence."""

from __future__ import annotations

import unittest

from factory_issue_pr_state_policy import (
    body_blocks_reconciliation,
    issue_labels_block_reconciliation,
    stage_from_trusted_prs,
)
from factory_work_policy import BLOCKED_LABELS


class FactoryIssuePrStatePolicyTests(unittest.TestCase):
    """Cover issue lifecycle precedence across trusted factory PR histories."""

    def test_merged_pr_plus_later_duplicate_open_stays_ready(self) -> None:
        """Keep completion when a later trusted duplicate PR is opened."""
        self.assertEqual(
            stage_from_trusted_prs([
                {"trusted": True, "merged": True, "state": "CLOSED"},
                {"trusted": True, "merged": False, "state": "OPEN"},
            ]),
            "factory:ready",
        )

    def test_merged_pr_plus_duplicate_closed_unmerged_stays_ready(self) -> None:
        """Keep completion when a later trusted duplicate closes unmerged."""
        self.assertEqual(
            stage_from_trusted_prs([
                {"trusted": True, "merged": True, "state": "CLOSED"},
                {"trusted": True, "merged": False, "state": "CLOSED"},
            ]),
            "factory:ready",
        )

    def test_open_trusted_pr_without_merged_history_is_review(self) -> None:
        """Put an issue in review when its trusted history has an open PR only."""
        self.assertEqual(
            stage_from_trusted_prs([
                {"trusted": True, "merged": False, "state": "CLOSED"},
                {"trusted": True, "merged": False, "state": "OPEN"},
            ]),
            "factory:review",
        )

    def test_only_closed_unmerged_attempts_are_claimable(self) -> None:
        """Return an issue to claimable backlog after all trusted attempts close."""
        self.assertEqual(
            stage_from_trusted_prs([
                {"trusted": True, "merged": False, "state": "CLOSED"},
            ]),
            "claimable",
        )

    def test_untrusted_factory_pr_does_not_affect_state(self) -> None:
        """Ignore untrusted factory-shaped PRs when deriving issue state."""
        self.assertEqual(
            stage_from_trusted_prs([
                {"trusted": False, "merged": True, "state": "CLOSED"},
                {"trusted": False, "merged": False, "state": "OPEN"},
            ]),
            "claimable",
        )

    def test_ralph_status_blocked_blocks_reconciliation(self) -> None:
        """Hold labels must skip factory label rewrites, including ralph-status:blocked."""
        self.assertTrue(
            issue_labels_block_reconciliation(
                ["factory", "factory:unowned", "ralph-status:blocked"]
            )
        )
        for label in BLOCKED_LABELS:
            self.assertTrue(issue_labels_block_reconciliation([label]), label)
        self.assertFalse(
            issue_labels_block_reconciliation(
                ["factory", "factory:unowned", "ralph-status:pending"]
            )
        )

    def test_manual_only_body_blocks_reconciliation(self) -> None:
        """A standalone manual-only PR body must skip factory label rewrites."""
        self.assertTrue(
            body_blocks_reconciliation(
                "<!-- factory-execution:manual-only -->\nHold this factory PR.\n"
            )
        )
        self.assertFalse(
            body_blocks_reconciliation("Ordinary autonomous factory work.\n")
        )
        self.assertFalse(
            body_blocks_reconciliation(
                "To opt out, use `<!-- factory-execution:manual-only -->`."
            )
        )
        self.assertTrue(body_blocks_reconciliation("Hold this factory PR.\nmanual-only\n"))
        self.assertFalse(body_blocks_reconciliation("Discussing a manual-only hold in prose.\n"))


if __name__ == "__main__":
    unittest.main()
