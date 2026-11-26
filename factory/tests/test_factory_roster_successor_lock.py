"""Regression for #5000: roster cadence must never hold the lease writer lock.

The dispatcher is a serialized assignment writer. Delays belong to a separate
workflow with no lease permission or job-level shared concurrency group.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.workflow_files import adopter_view, own_steps


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
DISPATCHER_PATH = WORKFLOWS / "fixed-model-factory-dispatch.yml"
SUCCESSOR_PATH = WORKFLOWS / "fixed-model-factory-roster-successor.yml"
LEASE_GROUP = "fixed-model-factory-dispatch"


def load_workflow(path: Path) -> dict:
    return yaml.safe_load(adopter_view(path.name))


def timer_step() -> dict:
    return load_workflow(SUCCESSOR_PATH)["jobs"]["wait_and_wake"]["steps"][0]


def test_dispatcher_releases_both_locks_before_roster_delay() -> None:
    dispatcher = load_workflow(DISPATCHER_PATH)
    job = dispatcher["jobs"]["dispatch"]
    assert job["concurrency"]["group"] == LEASE_GROUP

    steps = own_steps(job)
    cadence = next(
        step for step in steps
        if step.get("name") == "Queue roster successor outside assignment lease lock"
    )
    script = cadence["run"]

    # Waiting in this job blocks BOTH completion-drain lease acquisition and
    # additional explicit-worker dispatcher ingress.
    assert re.search(r"(?m)^\s*sleep\s+", script) is None
    assert "gh workflow run fixed-model-factory-dispatch.yml" not in script
    assert script.count("gh workflow run fixed-model-factory-roster-successor.yml") == 2
    assert 'delay_seconds=240' in script
    assert '"delay_seconds=$delay_seconds"' in script

    # Keep the original skip and quota/reset bounds. Do not turn the current
    # no-work stopping condition into an endless polling chain.
    assert 'BUDGET_UNREADABLE:-' in script
    assert 'QUOTA_DEFERRED:-' in script
    assert 'BUDGET_DEFERRED:-' in script
    assert 'dispatched_count" == "0" && "$dispatch_failed" == "false"' in script
    assert 'reset_epoch - now_epoch + 15' in script
    assert 'delay_seconds > 3700' in script
    assert "steps.api_budget.outputs.quota_deferred == 'true'" in cadence["if"]
    assert "inputs.mode == 'roster'" in cadence["if"]
    assert "!(inputs.target || inputs.provider)" in cadence["if"]

    # Scheduler is at the end, after every assignment and review operation;
    # only the store token removal follows it.
    assert cadence is steps[-2]
    assert steps[-1]["name"] == "Remove the factory store token"


def test_a_completion_pass_restarts_a_broken_roster_chain_only_when_none_waits() -> None:
    """Completion passes replaced the pending roster passes on Nov 26 and the chain died."""
    job = load_workflow(DISPATCHER_PATH)["jobs"]["dispatch"]
    cadence = next(
        step for step in own_steps(job)
        if step.get("name") == "Queue roster successor outside assignment lease lock"
    )
    assert "inputs.source_run_id != ''" in cadence["if"]
    script = cadence["run"]
    guard = script.index('if [[ -n "${COMPLETION_SOURCE:-}" ]]')
    assert guard < script.index("chain_roster_successor()")
    assert "fixed-model-factory-roster-successor.yml/runs" in script
    assert 'display_title == "Factory dispatch · roster"' in script
    assert cadence["env"]["COMPLETION_SOURCE"] == "${{ inputs.source_run_id }}"


def test_successor_is_isolated_and_read_only_except_actions_dispatch() -> None:
    workflow = load_workflow(SUCCESSOR_PATH)
    job = workflow["jobs"]["wait_and_wake"]
    triggers = workflow.get("on", workflow.get(True, {}))

    assert set(triggers) == {"workflow_dispatch"}
    assert set(workflow["permissions"]) == {"actions", "contents"}
    assert workflow["permissions"] == {"actions": "write", "contents": "read"}
    assert job["permissions"] == {"actions": "write", "contents": "read"}
    assert "concurrency" not in job

    # Coalesce duplicate timers independently of both the dispatcher ingress
    # and repository-wide lease lock, never cancel an active timer.
    assert workflow["concurrency"]["group"] == "fixed-model-factory-roster-successor"
    assert workflow["concurrency"]["queue"] == "single"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["concurrency"]["group"] != LEASE_GROUP
    assert job["timeout-minutes"] >= 63

    script = timer_step()["run"]
    assert re.search(r"(?m)^\s*sleep\s+", script)
    assert script.count("gh workflow run fixed-model-factory-dispatch.yml") == 1
    assert "--ref main -f mode=roster" in script
    assert "gh api" not in script
    assert "gh issue" not in script
    assert "gh pr" not in script


def _run_timer(tmp_path: Path, delay: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Execute the actual successor shell body using no-network stub commands."""
    trace = tmp_path / "calls"
    for name in ("sleep", "gh"):
        executable = tmp_path / name
        executable.write_text(
            "#!/bin/sh\nprintf '%s %s\\n' '" + name + "' \"$*\" >> \"$TRACE\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "TRACE": str(trace),
        "DELAY_SECONDS": delay,
        "GITHUB_REPOSITORY": "acme/widgets",
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", timer_step()["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    calls = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
    return result, calls


@pytest.mark.parametrize("delay", ["15", "240", "3700"])
def test_successor_waits_and_dispatches_one_roster_cycle(tmp_path: Path, delay: str) -> None:
    result, calls = _run_timer(tmp_path, delay)
    assert result.returncode == 0, result.stderr
    assert calls == [
        f"sleep {delay}",
        "gh workflow run fixed-model-factory-dispatch.yml "
        "--repo acme/widgets --ref main -f mode=roster",
    ]


@pytest.mark.parametrize("delay", ["", "0", "-1", "14", "3701", "abc", "2.5"])
def test_successor_rejects_invalid_delays_without_dispatch(tmp_path: Path, delay: str) -> None:
    result, calls = _run_timer(tmp_path, delay)
    assert result.returncode != 0
    assert calls == []
