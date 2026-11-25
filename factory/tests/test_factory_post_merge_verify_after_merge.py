"""Post-merge closure keeps an issue open until its verify-after-merge boxes are checked."""

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "scripts"))
github = importlib.import_module("factory_post_merge_closure_github")
closure = importlib.import_module("factory_post_merge_closure")
implementation_delivery = importlib.import_module("factory_implementation_delivery")
eligibility = importlib.import_module("factory_eligibility")
testing = importlib.import_module("factory_post_merge_closure_testing")

DEPLOY_LINE = "Verify the fix in an authenticated deployed browser and record the revision."
BODY = (
    f"## Acceptance criteria\n- [ ] The cover renders for a blocked series.\n- [ ] {DEPLOY_LINE}\n"
)
PR = {
    "branch": "factory/41-4017-kilo-auto",
    "body": "Closes #4017.",
    "state": "MERGED",
    "head": "abc123",
    "merge": "abc123",
}
DIFF = (["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n@@ -1 +1 @@\n-old = 1\n+new = 2\n")
LABELS = ["bug", "factory", "factory:ready", "factory:unowned"]


class VerifyAfterMergeClosureTests(testing.NoPersonReopenMixin, unittest.TestCase):
    def run_closure(self, body, *, state="OPEN", comments=(), closed_by_pr=False):
        calls = {"comments": [], "labels": [], "closed": 0, "gh": []}
        issue = {"state": state, "body": body, "labels": list(LABELS)}
        with (
            patch.object(github, "_issue_view", return_value=issue),
            patch.object(github, "_pr_info", return_value=dict(PR)),
            patch.object(github, "_issue_has_open_successor_pr", return_value=False),
            patch.object(implementation_delivery, "complete_pr_diff", return_value=DIFF),
            patch.object(github, "_repo_slug", return_value="owner/repo"),
            patch.object(github, "_native_open_blockers", return_value=[]),
            patch.object(github.eligibility, "parse_declared_dependencies", return_value=set()),
            patch.object(github, "_closed_by_pr", return_value=closed_by_pr),
            patch.object(github, "_has_dependency_block_marker", return_value=False),
            patch.object(github, "_has_closure_marker", return_value=False),
            patch.object(github, "_issue_comment_bodies", return_value=list(comments)),
            patch.object(
                github,
                "_comment_issue",
                side_effect=lambda _n, text: calls["comments"].append(text),
            ),
            patch.object(
                github,
                "_replace_labels",
                side_effect=lambda _n, labels: calls["labels"].append(labels),
            ),
            patch.object(
                github,
                "_close_issue",
                side_effect=lambda _n: calls.__setitem__("closed", calls["closed"] + 1),
            ),
            patch.object(github, "_run_gh", side_effect=lambda *args: calls["gh"].append(args)),
        ):
            result = closure.close_merged_pr_issue(5737)
        return result, calls

    def test_unchecked_deploy_criterion_keeps_issue_open_with_checklist(self):
        result, calls = self.run_closure(BODY)
        self.assertEqual(result, "verify-after-merge")
        self.assertEqual(calls["closed"], 0)
        self.assertEqual(len(calls["comments"]), 1)
        comment = calls["comments"][0]
        self.assertIn("factory-verify-after-merge-v1:pr-5737:issue-4017", comment)
        self.assertIn(f"- [ ] A2: Acceptance criteria: {DEPLOY_LINE}", comment)
        self.assertNotIn("A1", comment)
        labels = calls["labels"][-1]
        self.assertIn("ralph-status:done", labels)
        verdict = eligibility.explain_issue_eligibility(
            number=4017, state="OPEN", labels=set(labels), body=BODY, open_numbers={4017}
        )
        self.assertFalse(verdict.eligible)

    def test_repeat_visit_does_not_repeat_checklist(self):
        _, first = self.run_closure(BODY)
        result, calls = self.run_closure(BODY, comments=first["comments"])
        self.assertEqual(result, "verify-after-merge")
        self.assertEqual(calls["comments"], [])

    def test_checked_deploy_criterion_lets_issue_close(self):
        result, calls = self.run_closure(
            BODY.replace(f"- [ ] {DEPLOY_LINE}", f"- [x] {DEPLOY_LINE}")
        )
        self.assertEqual(result, "closed")
        self.assertEqual(calls["closed"], 1)

    def test_keyword_closed_issue_is_reopened_for_verification(self):
        result, calls = self.run_closure(BODY, state="CLOSED", closed_by_pr=True)
        self.assertEqual(result, "verify-after-merge")
        self.assertIn(("issue", "reopen", "4017", "--repo", "owner/repo"), calls["gh"])

    def test_issue_without_deploy_criterion_still_closes(self):
        result, calls = self.run_closure("## Scope\n- [ ] The cover renders.\n")
        self.assertEqual(result, "closed")
        self.assertEqual(calls["closed"], 1)
