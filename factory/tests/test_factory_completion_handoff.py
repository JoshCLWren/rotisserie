"""Regression tests for the trusted completion-action handoff into the worker."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
PRIMITIVES = SCRIPTS / "free-model-factory-worker-primitives.sh"
HEAD = "a" * 40


def _function(name: str) -> str:
    text = PRIMITIVES.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match is not None, f"{name} not found"
    return match.group(0)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def test_stage_to_action_mapping_matches_the_controller() -> None:
    script = f"""
set -euo pipefail
{_function('completion_action_for_stage')}
printf '%s|%s|%s|%s|%s\\n' \\
  "$(completion_action_for_stage factory:conflict)" \\
  "$(completion_action_for_stage factory:ci)" \\
  "$(completion_action_for_stage factory:changes-requested)" \\
  "$(completion_action_for_stage factory:review)" \\
  "$(completion_action_for_stage factory:building)"
"""
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "merge-conflict|ci-repair|review-findings|review|"


def test_worker_accepts_action_only_for_matching_stage_and_exact_head() -> None:
    prefix = f"""
set -euo pipefail
{_function('completion_action_for_stage')}
{_function('completion_action_matches_stage')}
{_function('trusted_completion_action_line')}
ASSIGNED_PR_STAGE=factory:ci
EXPECTED_HEAD={HEAD}
JEV_ACTION=ci-repair
JEV_ACTION_HEAD={HEAD}
"""
    accepted = _run(prefix + "trusted_completion_action_line\n")
    assert accepted.returncode == 0
    assert "ci-repair" in accepted.stdout
    assert HEAD in accepted.stdout

    stale = _run(prefix + f"JEV_ACTION_HEAD={'b' * 40}\ntrusted_completion_action_line\n")
    assert stale.returncode != 0
    assert stale.stdout == ""

    wrong_stage = _run(
        prefix.replace("ASSIGNED_PR_STAGE=factory:ci", "ASSIGNED_PR_STAGE=factory:review")
        + "trusted_completion_action_line\n"
    )
    assert wrong_stage.returncode != 0
    assert wrong_stage.stdout == ""

    missing_head = _run(prefix + "JEV_ACTION_HEAD=\ntrusted_completion_action_line\n")
    assert missing_head.returncode != 0


def test_merge_conflict_action_is_accepted_for_a_conflicted_repair_stage() -> None:
    script = f"""
set -euo pipefail
{_function('completion_action_for_stage')}
{_function('completion_action_matches_stage')}
{_function('trusted_completion_action_line')}
ASSIGNED_PR_STAGE=factory:changes-requested
EXPECTED_HEAD={HEAD}
JEV_ACTION=merge-conflict
JEV_ACTION_HEAD={HEAD}
trusted_completion_action_line
"""
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "merge-conflict" in result.stdout

    wrong = script.replace("JEV_ACTION=merge-conflict", "JEV_ACTION=review")
    rejected = _run(wrong)
    assert rejected.returncode != 0
    assert rejected.stdout == ""
