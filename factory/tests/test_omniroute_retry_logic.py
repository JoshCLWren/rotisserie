"""Regression tests for the issue #2155 OmniRoute stream retry logic."""

from __future__ import annotations

import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / ".github" / "scripts"

TRANSIENT_CLASSIFIER_SCRIPTS = [
    "free-model-factory-worker-primitives.sh",
    "nvidia-factory-worker.sh",
    "omniroute-factory-worker.sh",
]

RETRY_GUARD_SCRIPTS = [
    "free-model-factory-worker-primitives.sh",
    "free-model-factory-worker.sh",
    "nvidia-factory-worker.sh",
    "omniroute-factory-worker.sh",
]

RETRY_GUARD_RE = re.compile(r"\(\(\s*\$\(remaining\)\s*>\s*540\s*\)\)\s*\|\|\s*break")


def _script(name: str) -> str:
    """Read the worker script for the given basename."""
    return (SCRIPTS_DIR / name).read_text()


def test_early_stream_termination_is_classified_as_transient() -> None:
    """Every worker must classify stream_early_eof as a transient agent failure."""
    for name in TRANSIENT_CLASSIFIER_SCRIPTS:
        assert "stream_early_eof" in _script(name), (
            f"{name} must classify stream_early_eof as transient"
        )


def test_retry_guard_uses_540_second_minimum() -> None:
    """The bounded retry must be permitted above the 540-second minimum budget."""
    for name in RETRY_GUARD_SCRIPTS:
        text = _script(name)
        assert RETRY_GUARD_RE.search(text), (
            f"{name} must retry above the 540-second minimum budget"
        )
        assert not re.search(r"\(\(\s*\$\(remaining\)\s*>\s*600\s*\)\)", text), (
            f"{name} must not retain the old 600-second retry threshold"
        )


def test_retry_remains_fail_closed_for_dirty_worktree_and_exhausted_attempts() -> None:
    """Dirty worktrees and exhausted attempts must still break without retrying."""
    clean_worktree_guards = {
        "free-model-factory-worker-primitives.sh": '[[ -z "$(git status --porcelain)" ]] || break',
        "free-model-factory-worker.sh": '[[ -z "$(git status --porcelain)" ]] || break',
        "nvidia-factory-worker.sh": '[[ -n "$(git status --porcelain)" ]]',
        "omniroute-factory-worker.sh": '[[ -z "$(git status --porcelain)" ]] || break',
    }
    for name in RETRY_GUARD_SCRIPTS:
        text = _script(name)
        assert clean_worktree_guards[name] in text, (
            f"{name} must bail out of retrying on a dirty worktree"
        )
        assert "(( agent_attempt < MAX_AGENT_ATTEMPTS )) || break" in text, (
            f"{name} must cap retries at MAX_AGENT_ATTEMPTS"
        )
        assert RETRY_GUARD_RE.search(text), (
            f"{name} must keep the bounded 540-second retry guard"
        )