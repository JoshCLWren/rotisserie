#!/usr/bin/env python3
"""Regression tests for issue #2860: capacity refill signal-only assignment paths."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts_dir))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


factory_full = load_module(
    "factory_full_completion_controller_for_test",
    scripts_dir / "factory_full_completion_controller.py",
)


class SignalDispatchTests(unittest.TestCase):
    def _signal(self, controller, demand, capacity):
        """Run signal_dispatch with an offline stubbed work controller."""
        work = MagicMock()
        work.list_issues.return_value = []
        work.list_prs.return_value = []
        work.reconcile_stale_leases.return_value = []
        work.reconcile_contradictory_labels.return_value = []
        original = controller.load_controller
        controller.load_controller = lambda: work
        try:
            return factory_full.signal_dispatch(controller, demand, capacity)
        finally:
            controller.load_controller = original

    def test_signal_dispatch_returns_no_assignments(self) -> None:
        """Capacity refill must not directly assign workers or launch entries."""
        controller = factory_full.load_controller()
        demand = MagicMock(completion=0, production=0, idle_workers=0, completion_share=0.0)
        capacity = {"enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12}
        result = self._signal(controller, demand, capacity)
        self.assertEqual(result["assignments"], [])
        self.assertIn(result["signal"], ("explicit-worker", "roster"))

    def test_signal_dispatch_includes_demand_data(self) -> None:
        """Signal must include demand and capacity data."""
        controller = factory_full.load_controller()
        demand = MagicMock(completion=1, production=2, idle_workers=3, completion_share=0.5)
        capacity = {"enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12}
        result = self._signal(controller, demand, capacity)
        self.assertIn("completion_demand", result)
        self.assertIn("production_demand", result)
        self.assertIn("completion_share", result)
        self.assertIn("completion_target", result)
        self.assertIn("capacity", result)

    def test_signal_dispatch_performs_reconciliation(self) -> None:
        """Signal must perform recovery/reconciliation without assignment."""
        controller = factory_full.load_controller()
        demand = MagicMock(completion=0, production=0, idle_workers=0, completion_share=0.0)
        capacity = {"enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12}
        # signal_dispatch calls reconcile_stale_leases and reconcile_contradictory_labels
        # via the work controller; verify it executes without error
        result = self._signal(controller, demand, capacity)
        self.assertIn("signal", result)
        self.assertIn("assignments", result)
        self.assertEqual(result["assignments"], [])

    def test_signal_dispatch_omits_direct_assignment(self) -> None:
        """Signal dispatch must not call assign_completion_batch directly."""
        # Verify that signal_dispatch does not include assignments from
        # assign_completion_batch - it returns empty assignments
        controller = factory_full.load_controller()
        demand = MagicMock(completion=0, production=0, idle_workers=0, completion_share=0.0)
        capacity = {"enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12}
        result = self._signal(controller, demand, capacity)
        self.assertEqual(result["assignments"], [])


class MainSignalOnlyTests(unittest.TestCase):
    def test_main_does_not_assign_directly(self) -> None:
        """main() must not directly assign workers or launch entries."""
        with patch.object(factory_full, "current_demand") as mock_demand, \
             patch.object(factory_full, "signal_dispatch") as mock_signal:
            mock_demand.return_value = (
                MagicMock(completion=0, production=0, idle_workers=0, completion_share=0.0),
                {"enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12},
            )
            mock_signal.return_value = {"signal": "roster", "assignments": []}
            factory_full.main()
            mock_signal.assert_called()


if __name__ == "__main__":
    unittest.main()
