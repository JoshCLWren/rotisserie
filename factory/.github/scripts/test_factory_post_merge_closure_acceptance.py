"""Cover the #1620 explicit product-acceptance closure contract."""

from __future__ import annotations

import unittest

import factory_implementation_delivery as implementation_delivery
import factory_post_merge_closure_github as github
from factory_post_merge_closure import close_merged_pr_issue
from factory_post_merge_closure_policy import (
    _child_numbers,
    _issue_is_acceptance_parent,
    _unmet_parent_criteria,
    acceptance_parent_may_close,
)


class FactoryPostMergeClosureAcceptanceTests(unittest.TestCase):
    """Cover the #1620 explicit product-acceptance closure contract."""

    # Issue #1620 closure-critical contract: explicit product acceptance.

    def test_epic_label_alone_is_an_acceptance_parent(self) -> None:
        """An epic/PRD always needs a verdict, even with no child graph."""
        self.assertTrue(_issue_is_acceptance_parent(["epic", "factory"], "Ship it."))
        self.assertTrue(_issue_is_acceptance_parent(["prd", "factory"], ""))
        self.assertFalse(_issue_is_acceptance_parent(["enhancement", "factory"], "Ship it."))

    def test_acceptance_parent_language_without_epic_label_is_protected(self) -> None:
        """The #1615 incident shape: contract in the body, no epic/prd label."""
        body = (
            "This is the acceptance parent for CBL adoption.\n"
            "- [x] #2127 — transactional adoption\n"
            "- [ ] #2128 — production browser UI\n"
        )
        self.assertTrue(_issue_is_acceptance_parent(["enhancement", "factory"], body))

    def test_ordinary_child_references_are_not_a_parent_contract(self) -> None:
        """Casual cross-references without acceptance language stay ordinary."""
        body = "Refs #2127 for context.\n- [x] polish\n- [ ] follow-up tweak"
        self.assertFalse(_issue_is_acceptance_parent(["enhancement", "factory"], body))

    def test_executable_child_defining_acceptance_split_is_not_a_parent(self) -> None:
        """#3037/#2718/#2128: implementation work is not an acceptance parent.

        Each of these issues carries a checklist and defers the operator
        production-acceptance pass to a separate issue. Treating the deferral
        as a parent contract would strand a user-reported bug and its delivery
        PR closure, so only a self-declared parent contract counts.
        """
        deferred = (
            "## Production acceptance split\n"
            "- [ ] #3042 owns the production-wide audit and reconciliation.\n"
            "- [ ] The invariant is enforced at the database layer.\n"
            "- [ ] API paths handle an already-present issue predictably.\n"
        )
        self.assertFalse(_issue_is_acceptance_parent(["bug", "user-reported"], deferred))
        self.assertFalse(
            _issue_is_acceptance_parent(
                ["enhancement"],
                "Refs #2129 so it can use this path for production acceptance.\n"
                "- [ ] Reader can adopt after one confirmation\n",
            )
        )

    def test_acceptance_parent_language_without_child_graph_is_not_a_parent(self) -> None:
        """Parent language alone, without a declared child graph, stays ordinary."""
        body = "This issue is the acceptance parent for the rollout.\n- [ ] ship it\n"
        self.assertFalse(_issue_is_acceptance_parent(["enhancement", "factory"], body))

    def test_executable_child_issue_still_closes_on_delivery_merge(self) -> None:
        """The guard must not refuse ordinary closure for deferred-acceptance work."""
        from unittest.mock import patch

        mod = github
        issue_body = (
            "## Production acceptance split\n"
            "- [ ] #3042 owns the production-wide audit.\n"
            "- [ ] Uniqueness is enforced at the database layer.\n"
        )
        with patch.object(
            mod,
            "_issue_view",
            return_value={
                "state": "OPEN",
                "body": issue_body,
                "labels": ["bug", "user-reported", "factory", "factory:unowned"],
            },
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/21-3037-opencode-free",
                    "body": "Closes #3037.",
                    "state": "MERGED",
                },
            ):
                with patch.object(
                    mod, "_issue_has_open_successor_pr", return_value=False
                ), patch.object(
                    implementation_delivery, "complete_pr_diff",
                    return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                  "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                ), patch.object(mod, "_repo_slug", return_value="owner/repo"):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2150, dry_run=True)
        self.assertTrue(result.startswith("dry-run:"), result)

    def test_unmet_parent_criterion_blocks_despite_closed_children(self) -> None:
        """All children closed but one unchecked parent criterion stays open."""
        body = (
            "- [x] #2127\n"
            "- [x] #2128\n"
            "- [ ] Roll honors applicable adopted hard boundaries\n"
            "- [x] Reload preserves plan intent\n"
        )
        self.assertEqual(
            _unmet_parent_criteria(body),
            ["Roll honors applicable adopted hard boundaries"],
        )
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"],
                body,
                1615,
                {2127: "CLOSED", 2128: "CLOSED"},
                ["<!-- product-acceptance:v1 -->\n### Verdict\n ACCEPTED"],
            )
        )

    def test_checked_parent_criteria_do_not_block(self) -> None:
        """Checked criteria (either case) leave no unmet contract."""
        body = "- [x] #2127\n- [X] Roll honors hard boundaries\n- [x] Reload preserves intent\n"
        self.assertEqual(_unmet_parent_criteria(body), [])

    def test_criterion_quoting_an_issue_is_still_a_criterion(self) -> None:
        """Only leading ``#NNN`` child declarations defer to the child gate."""
        body = (
            "- [x] #2127\n"
            "- [ ] Adoption uses the #2551 Dependency model\n"
        )
        self.assertEqual(
            _unmet_parent_criteria(body),
            ["Adoption uses the #2551 Dependency model"],
        )
        self.assertEqual(_child_numbers(body, 1615), {2127})

    def test_acceptance_parent_with_verdict_may_close(self) -> None:
        """A fully verified parent with an ACCEPTED report may transition."""
        body = "- [x] #2127\n- [x] #2128\n- [x] Roll honors hard boundaries\n"
        report = (
            "<!-- product-acceptance:v1 -->\n"
            "## Product acceptance report\n"
            "### Verdict\n ACCEPTED"
        )
        self.assertTrue(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615,
                {2127: "CLOSED", 2128: "CLOSED"}, [report],
            )
        )

    def test_child_count_alone_never_satisfies_acceptance(self) -> None:
        """Closed children without any verdict report cannot complete a parent."""
        body = "- [x] #2127\n- [x] #2128\n- [x] Roll honors hard boundaries\n"
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615,
                {2127: "CLOSED", 2128: "CLOSED"}, [],
            )
        )
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615,
                {2127: "CLOSED", 2128: "CLOSED"},
                ["LGTM, CI green, coverage 94%."],
            )
        )

    def test_failed_verdict_keeps_parent_open(self) -> None:
        """NOT ACCEPTED retains the parent even with closed children."""
        body = "- [x] #2127\n- [x] Roll honors hard boundaries\n"
        report = "<!-- product-acceptance:v1 -->\n### Verdict\n NOT ACCEPTED"
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615, {2127: "CLOSED"}, [report],
            )
        )

    def test_latest_verdict_wins(self) -> None:
        """A newer failed report revokes an older acceptance."""
        body = "- [x] #2127\n"
        accepted = "<!-- product-acceptance:v1 -->\n### Verdict\n ACCEPTED"
        failed = "<!-- product-acceptance:v1 -->\n### Verdict\n NOT ACCEPTED"
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615, {2127: "CLOSED"}, [accepted, failed],
            )
        )
        self.assertTrue(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615, {2127: "CLOSED"}, [failed, accepted],
            )
        )

    def test_open_child_blocks_despite_accepted_verdict(self) -> None:
        """A stale ACCEPTED report cannot close over a reopened child."""
        body = "- [x] #2127\n- [ ] #2128\n"
        report = "<!-- product-acceptance:v1 -->\n### Verdict\n ACCEPTED"
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615,
                {2127: "CLOSED", 2128: "OPEN"}, [report],
            )
        )

    def test_non_parent_never_satisfies_acceptance_gate(self) -> None:
        """Ordinary issues stay outside the acceptance gate entirely."""
        self.assertFalse(
            acceptance_parent_may_close(
                ["bug", "factory"], "- [x] fixed", 500, {},
                ["<!-- product-acceptance:v1 -->\n ACCEPTED"],
            )
        )

    def test_merge_closure_refuses_verified_parent_without_verdict_path(self) -> None:
        """Merge automation defers even fully-checked parents to the verdict path.

        A duplicate factory PR re-closing already-delivered child work must not
        satisfy product acceptance: without a verdict comment on the issue, the
        merge path parks the parent instead of closing it.
        """
        from unittest.mock import patch

        mod = github
        issue_body = (
            "This issue is a product contract and acceptance parent.\n"
            "- [x] #2127 — transactional selective materialization\n"
            "- [x] #2128 — production CBL browser/import-review UI\n"
            "- [x] Reader can adopt with one compact confirmation\n"
        )
        with patch.object(mod, "_issue_view") as mock_view:
            mock_view.side_effect = [
                {
                    "state": "OPEN",
                    "body": issue_body,
                    "labels": ["epic", "factory", "factory:ready", "factory:unowned"],
                },
                {"state": "CLOSED", "body": "", "labels": []},
                {"state": "CLOSED", "body": "", "labels": []},
            ]
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/58-1615-duplicate",
                    "body": "Closes #1615.",
                    "state": "MERGED",
                },
            ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2141, dry_run=True)
        self.assertEqual(result, "acceptance-parent-requires-verdict")

    def test_merge_closure_blocks_parent_with_unmet_criterion(self) -> None:
        """A parent with an unchecked criterion reports incomplete, not closable."""
        from unittest.mock import patch

        mod = github
        issue_body = (
            "## Parent acceptance criteria\n"
            "- [x] #2127 — transactional selective materialization\n"
            "- [ ] Roll honors applicable adopted hard boundaries\n"
        )
        with patch.object(
            mod,
            "_issue_view",
            return_value={
                "state": "OPEN",
                "body": issue_body,
                "labels": ["epic", "factory", "factory:unowned"],
            },
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/58-1615-work",
                    "body": "Closes #1615.",
                    "state": "MERGED",
                },
            ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2142, dry_run=True)
        self.assertEqual(result, "acceptance-parent-incomplete")

    def test_merge_closure_refuses_manual_only_acceptance_parent(self) -> None:
        """An operator acceptance/cutover parent is never closed by automation."""
        from unittest.mock import patch

        mod = github
        issue_body = (
            "Part of #1615. Child D. **Manual-only production acceptance/cutover.**\n"
            "<!-- factory-execution:manual-only -->\n"
            "- [ ] Ultimate Universe has one canonical Reading Plan\n"
            "- [ ] CBL source positions/provenance are preserved\n"
        )
        with patch.object(
            mod,
            "_issue_view",
            return_value={
                "state": "OPEN",
                "body": issue_body,
                "labels": ["enhancement", "factory", "factory:blocked", "factory:unowned"],
            },
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/46-2129-opencode-free",
                    "body": "Closes #2129.",
                    "state": "MERGED",
                },
            ):
                with patch.object(mod, "_issue_has_open_successor_pr") as mock_successor:
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2143, dry_run=True)
        self.assertEqual(result, "manual-only-issue")
        mock_successor.assert_not_called()
