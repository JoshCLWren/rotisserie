"""Shared loader and base case for the fixed-model classifier tests."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "classify_fixed_model_run", Path(__file__).with_name("classify-fixed-model-run.py")
)
assert _SPEC is not None and _SPEC.loader is not None
classifier = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = classifier
_SPEC.loader.exec_module(classifier)
Result = classifier.Result
classify = classifier.classify


class ClassifierCase(unittest.TestCase):
    BASE = (
        "Factory 32 locked: source=omniroute-opencode model=oc/big-pickle "
        "runtime=omniroute/oc/big-pickle minute=:15 scheduler=slot-15 configured=true\n"
    )

    def assert_canonical(self, result: Result) -> None:
        """Assert one emitted class belongs to the durable factory contract."""
        self.assertIn(result.outcome_class, classifier.CANONICAL_OUTCOME_CLASSES)
