"""A review whose edits fail the fast checks still submits its verdict."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / ".github/scripts/free-model-factory-worker.sh"


def _blocked_gate(stage: str) -> str:
    text = WORKER.read_text()
    gate = re.search(
        r'^if \[\[ -n "\$FACTORY_PUSH_BLOCKED_REASON".*?^fi$', text, re.S | re.M
    )
    assert gate
    script = f"""
release_pr_and_issue() {{ echo "release $3 $4"; }}
record_terminal_outcome() {{ echo "outcome $1"; }}
FACTORY_PUSH_BLOCKED_REASON=fast-checks-failed ASSIGNED_PR_STAGE={stage}
NUMBER=5777 BRANCH=b
{gate[0]}
echo reached-verdict-path
"""
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    ).stdout


def test_review_stage_reaches_the_verdict_path() -> None:
    """Runs 45585302957 and 45631638578 lost FACTORY_GATE_NOT_READY verdicts here."""
    out = _blocked_gate("factory:review")
    assert "outcome work_failure" not in out
    assert "reached-verdict-path" in out


def test_repair_stage_still_ends_as_a_validation_failure() -> None:
    out = _blocked_gate("factory:changes-requested")
    assert "release factory:changes-requested failed-validation" in out
    assert "outcome work_failure" in out
    assert "reached-verdict-path" not in out


def test_review_downgrade_for_blocked_push_still_exists() -> None:
    text = WORKER.read_text()
    assert "if [[ \"$verdict\" == 'approve' && -n \"$FACTORY_PUSH_BLOCKED_REASON\" ]]; then" in text
