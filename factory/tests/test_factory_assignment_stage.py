"""The worker's assigned-PR stage check must match the controller's projection.

Production evidence (runs 43965366691..43967668792): the reconciler left PR
#5241 stageless, the controller exported ``"stage": null``, and the worker
demanded a label literally named "null". Every dispatch exited 3 before any
inference and re-queued the same PR.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / ".github/scripts/free-model-factory-worker.sh"


def _stage_program() -> str:
    worker = WORKER.read_text(encoding="utf-8")
    block = worker.split('ASSIGNED_PR_STAGE="$(jq -r \'.stage // empty\' <<< "$assignment")"', 1)[1]
    match = re.search(r"jq -e --arg stage \"\$ASSIGNED_PR_STAGE\" '(.*?)' <<< \"\$target\"", block, re.S)
    assert match, "worker stage check not found"
    return match.group(1)


def _stage_of(labels: list[str]) -> str | None:
    import importlib
    import sys

    sys.path.insert(0, str(ROOT / ".github/scripts"))
    policy = importlib.import_module("factory_work_policy")
    return policy.stage_of(labels)


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the worker")
@pytest.mark.parametrize(
    ("live_labels", "assigned_labels", "accepted"),
    [
        (["factory"], ["factory"], True),
        (["factory", "factory:review"], ["factory"], False),
        (["factory"], ["factory", "factory:review"], False),
        (["factory", "factory:review"], ["factory", "factory:review"], True),
        (["factory", "factory:changes-requested"], ["factory", "factory:review"], False),
    ],
)
def test_worker_stage_check_matches_controller_stage_projection(
    live_labels: list[str], assigned_labels: list[str], accepted: bool,
) -> None:
    assignment = {"stage": _stage_of(assigned_labels)}
    stage = subprocess.run(
        ["jq", "-r", ".stage // empty"], input=json.dumps(assignment),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    target = {"labels": [{"name": label} for label in live_labels]}
    result = subprocess.run(
        ["jq", "-e", "--arg", "stage", stage, _stage_program()],
        input=json.dumps(target), capture_output=True, text=True, check=False,
    )
    assert (result.returncode == 0) is accepted
    assert accepted is (_stage_of(live_labels) == assignment["stage"])
