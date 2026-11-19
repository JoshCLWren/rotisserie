"""Regression tests for the free-model factory validator's runner contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

VALIDATOR = Path(".github/scripts/validate-free-model-factories.py")
REMOVED_RUNNER_TEXT = "starves restored free-code lanes"


def test_validator_no_longer_requires_removed_runner_text() -> None:
    """Guard #4483: the workflow dropped this wording, so the validator must not assert it."""
    assert REMOVED_RUNNER_TEXT not in VALIDATOR.read_text(encoding="utf-8")


def test_validator_accepts_the_current_runner_and_factory_sources() -> None:
    """Run the real validator so stale runner text fails PR CI, not only the schedule."""
    completed = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
