#!/usr/bin/env python3
"""Regression tests for trusted factory issue-state precedence."""

from __future__ import annotations

import unittest

from factory_issue_pr_state_policy import stage_from_trusted_prs


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


if __name__ == "__main__":
    unittest.main()
