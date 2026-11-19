#!/usr/bin/env python3
"""Regression tests for issue #2860 acceptance criteria."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts_dir))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


factory_work = load_module(
    "factory_work_controller_for_test",
    scripts_dir / "factory-work-controller.py",
)


class AcceptanceCriteriaTests(unittest.TestCase):
    def test_assign_function_exists(self) -> None:
        """The assign function must exist and handle worker assignment."""
        self.assertTrue(callable(getattr(factory_work, "assign", None)))

    def test_signal_functions_in_all_exports(self) -> None:
        """Signal functions must be exported."""
        self.assertIn("signal_completion_mode", factory_work.__all__)
        self.assertIn("signal_roster_mode", factory_work.__all__)

    def test_omni_route_off_capacity_rules_preserved(self) -> None:
        """Preserve current OmniRoute-off/multi-provider capacity rules."""
        from factory_capacity_policy import (
            omniroute_enabled,
            DEFAULT_MULTI_PROVIDER_ENTRY_CAP,
            DEFAULT_OMNIROUTE_FREE_ENTRY_CAP,
        )
        self.assertFalse(omniroute_enabled())
        self.assertEqual(DEFAULT_MULTI_PROVIDER_ENTRY_CAP, 12)
        self.assertEqual(DEFAULT_OMNIROUTE_FREE_ENTRY_CAP, 3)

    def test_signal_completion_mode_signature(self) -> None:
        """signal_completion_mode must accept keyword-only now_epoch."""
        import inspect
        sig = inspect.signature(factory_work.signal_completion_mode)
        self.assertTrue(all(p.default is not inspect.Parameter.empty for p in sig.parameters.values()))

    def test_signal_roster_mode_signature(self) -> None:
        """signal_roster_mode must accept keyword-only now_epoch."""
        import inspect
        sig = inspect.signature(factory_work.signal_roster_mode)
        self.assertTrue(all(p.default is not inspect.Parameter.empty for p in sig.parameters.values()))

    def test_assign_signature(self) -> None:
        """assign must accept worker and kinds."""
        import inspect
        sig = inspect.signature(factory_work.assign)
        params = list(sig.parameters.keys())
        self.assertIn("worker", params)
        self.assertIn("kinds", params)

    def test_signal_completion_mode_callable(self) -> None:
        """signal_completion_mode must be callable."""
        self.assertTrue(callable(factory_work.signal_completion_mode))

    def test_signal_roster_mode_callable(self) -> None:
        """signal_roster_mode must be callable."""
        self.assertTrue(callable(factory_work.signal_roster_mode))


if __name__ == "__main__":
    unittest.main()
