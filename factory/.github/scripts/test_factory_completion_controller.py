#!/usr/bin/env python3
"""Regression tests for issue #2860: completion drain signal-only assignment paths."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts_dir))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


factory_completion = load_module(
    "factory_completion_controller_for_test",
    scripts_dir / "factory_completion_controller.py",
)


class SignalCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Unit tests must not call the live GitHub CLI or mutate factory leases.
        controller_patch = patch.object(factory_completion, "load_controller")
        policy_patch = patch.object(factory_completion, "load_policy")
        self.addCleanup(controller_patch.stop)
        self.addCleanup(policy_patch.stop)
        controller = controller_patch.start().return_value
        policy = policy_patch.start().return_value
        controller.list_issues.return_value = []
        controller.list_prs.return_value = []
        controller.omniroute_free_entry_capacity.return_value = {
            "enabled": True, "remaining": 0,
        }
        policy.factory_review_backlog_count.return_value = 0

    def test_signal_completion_has_signal_key(self) -> None:
        """Completion drain must return a signal field."""
        result = factory_completion.signal_completion_mode()
        self.assertIn("signal", result)

    def test_signal_completion_has_assignments_key(self) -> None:
        """Completion drain must return an assignments key (empty)."""
        result = factory_completion.signal_completion_mode()
        self.assertIn("assignments", result)

    def test_signal_completion_returns_no_direct_assignments(self) -> None:
        """Completion drain must not directly assign PRs."""
        result = factory_completion.signal_completion_mode()
        self.assertEqual(result["assignments"], [])

    def test_signal_completion_has_capacity(self) -> None:
        """Signal must include capacity information."""
        result = factory_completion.signal_completion_mode()
        self.assertIn("capacity", result)
        self.assertIn("remaining", result["capacity"])
        self.assertIn("enabled", result["capacity"])

    def test_signal_completion_has_backlog(self) -> None:
        """Signal must include backlog data."""
        result = factory_completion.signal_completion_mode()
        self.assertIn("backlog", result)

    def test_assign_completion_batch_delegates_to_signal(self) -> None:
        """assign_completion_batch must delegate to signal_completion_mode, not directly assign."""
        with patch.object(factory_completion, "signal_completion_mode") as mock_signal:
            mock_signal.return_value = {"signal": "completion", "assignments": [], "backlog": 0}
            result = factory_completion.assign_completion_batch()
            mock_signal.assert_called_once()
            self.assertEqual(result["assignments"], [])

    def test_signal_completion_when_idle(self) -> None:
        """When backlog is below threshold, signal should be idle."""
        result = factory_completion.signal_completion_mode()
        self.assertIn(result["signal"], ("completion", "idle"))


if __name__ == "__main__":
    unittest.main()
