"""Regression coverage for per-attempt factory worker log isolation."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / ".github" / "scripts" / "free-model-factory-worker.sh"


def test_worker_clears_stale_logs_before_recording_outcomes() -> None:
    """A prior run's throttle must not poison the current attempt outcome."""
    text = WORKER.read_text(encoding="utf-8")

    clear_log = ': > "$worker_log"'
    assert clear_log in text
    assert text.index(clear_log) < text.index("record_terminal_outcome()")
    assert '"/tmp/opencode-factory-${WORKER}.sanitized.log"' in text
    assert '"/tmp/opencode-factory-${WORKER}-verdict-recovery.log"' in text
