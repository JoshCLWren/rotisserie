"""Regression tests for the automatic OpenCode reviewer safety contract."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REVIEWER_AGENT = ROOT / ".opencode" / "agents" / "pr-reviewer.md"
OPENCODE_WORKFLOW = ROOT / ".github" / "workflows" / "opencode.yml"


def _load_agent_permission() -> dict:
    """Load the pr-reviewer agent's permission block from its frontmatter."""
    text = REVIEWER_AGENT.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise AssertionError("pr-reviewer agent must start with YAML frontmatter")
    _, frontmatter, _ = text.split("---", 2)
    config = yaml.safe_load(frontmatter)
    return config["permission"]


class OpenCodeReviewPolicyTests(unittest.TestCase):
    """Protect structural read-only review and atomic factory metadata updates."""

    def test_reviewer_cannot_edit_or_run_arbitrary_shell_commands(self) -> None:
        """Require structural denial of edits and arbitrary shell execution."""
        permission = _load_agent_permission()
        self.assertEqual(permission["edit"], "deny")
        self.assertEqual(permission["task"], "deny")
        self.assertEqual(permission["external_directory"], "deny")
        self.assertEqual(permission["question"], "deny")
        self.assertEqual(permission["bash"]["*"], "deny")
        self.assertEqual(
            permission["bash"]["gh api --method POST repos/*/pulls/*/comments*"],
            "allow",
        )

    def test_workflow_reconciles_labels_atomically_and_preserves_owner(self) -> None:
        """Reject additive/sequential stage mutation or owner-dropping replacement."""
        workflow = OPENCODE_WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("gh api --method DELETE", workflow)
        self.assertIn('^factory:(unowned|local|([1-9]|[1-3][0-9]|4[0-6]))$', workflow)
        self.assertIn('first // "factory:unowned"', workflow)
        self.assertEqual(workflow.count('gh api --method PUT'), 2)
        self.assertNotIn(
            'gh api --method POST \\\n            "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels"',
            workflow,
        )
        self.assertGreaterEqual(workflow.count('"factory", $owner'), 2)

    def test_reviewer_has_no_merge_or_label_mutation_command(self) -> None:
        """Keep automatic review limited to posting inline findings."""
        permission = _load_agent_permission()
        allowed_bash = {
            command
            for command, decision in permission["bash"].items()
            if decision == "allow"
        }
        self.assertFalse(any("merge" in command for command in allowed_bash))
        self.assertFalse(any("labels" in command for command in allowed_bash))


if __name__ == "__main__":
    unittest.main()
