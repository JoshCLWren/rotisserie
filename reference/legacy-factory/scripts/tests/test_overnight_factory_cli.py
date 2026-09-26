"""Regression tests for the overnight factory supervisor CLI."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "comic-pile-opencode-factory-overnight.sh"


class OvernightFactoryCliTest(unittest.TestCase):
    """Keep the command parser and user-facing help synchronized."""

    def test_help_documents_command_first_factory_options(self) -> None:
        """Factory options must be shown after start or run, never as commands."""
        result = subprocess.run(
            ["bash", str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn(
            "Additional factory options are passed to "
            "comic-pile-opencode-factory.sh after the selected command.",
            result.stdout,
        )
        self.assertIn(
            "comic-pile-opencode-factory-overnight.sh start --idle-seconds 30",
            result.stdout,
        )
        self.assertIn(
            "comic-pile-opencode-factory-overnight.sh run --idle-seconds 30",
            result.stdout,
        )
        self.assertNotIn("after --watch", result.stdout)
        self.assertNotIn("comic-pile-opencode-factory-overnight.sh --watch", result.stdout)

    def test_watch_without_command_is_rejected_with_command_usage(self) -> None:
        """The former ambiguous invocation should fail and show valid commands."""
        result = subprocess.run(
            ["bash", str(SCRIPT), "--watch"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("<start|stop|status|run> [factory options]", result.stderr)
        self.assertIn("ERROR: unknown command: --watch", result.stderr)


if __name__ == "__main__":
    unittest.main()
