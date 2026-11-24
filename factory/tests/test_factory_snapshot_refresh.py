"""The dispatcher keeps the reliability snapshot under about 15 minutes old."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from factory_snapshot_age import UNKNOWN_AGE, age_seconds  # noqa: E402

WORKFLOW = ROOT.parent / ".github/workflows/fixed-model-factory-dispatch.yml"
STEP = "Refresh the reliability snapshot when it is stale"
NOW = datetime(2026, 11, 24, 13, 40, tzinfo=UTC)


def test_age_reads_the_python_isoformat_stamp() -> None:
    assert age_seconds("2026-11-24T05:54:43.956493+00:00", NOW) == 27916
    assert age_seconds("2026-11-24T13:39:00Z", NOW) == 60
    assert age_seconds("", NOW) == UNKNOWN_AGE
    assert age_seconds("not a time", NOW) == UNKNOWN_AGE


def _step() -> dict[str, object]:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    steps = jobs["dispatch"]["steps"]
    assert steps[-2]["name"] == STEP
    assert steps[-1]["name"] == "Queue roster successor outside assignment lease lock"
    return steps[-2]


def test_refresh_runs_after_all_assignment_work_and_never_fails_the_tick() -> None:
    step = _step()
    assert step["if"] == "always()"
    assert step["continue-on-error"] is True
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["env"]["STALE_AFTER_SECONDS"] == "840"


def _run(tmp_path: Path, generated: str, pending: int) -> list[str]:
    calls = tmp_path / "calls"
    stats = json.dumps({"generated_at": generated})
    fake = tmp_path / "gh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {calls}\n'
        'case "$*" in\n'
        f"  *contents/factory-stats.json*) echo '{stats}' ;;\n"
        f"  *factory-status-pages.yml/runs*) echo {pending} ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    scripts = tmp_path / ".github/scripts"
    scripts.mkdir(parents=True)
    (scripts / "factory_snapshot_age.py").write_text(
        (ROOT / ".github/scripts/factory_snapshot_age.py").read_text()
    )
    env = {
        **os.environ,
        "FACTORY_REAL_GH": str(fake),
        "GITHUB_REPOSITORY": "acme/widgets",
        "STALE_AFTER_SECONDS": "840",
    }
    subprocess.run(
        ["bash", "-c", str(_step()["run"])], cwd=tmp_path, env=env, check=True, capture_output=True
    )
    return calls.read_text().splitlines()


@pytest.mark.parametrize(
    ("age", "pending", "dispatched"),
    [
        (timedelta(hours=7), 0, True),
        (timedelta(minutes=5), 0, False),
        (timedelta(hours=7), 1, False),
    ],
)
def test_refresh_dispatches_only_a_stale_snapshot_without_a_pending_run(
    tmp_path: Path, age: timedelta, pending: int, dispatched: bool
) -> None:
    generated = (datetime.now(UTC) - age).isoformat()
    calls = _run(tmp_path, generated, pending)
    sent = [call for call in calls if call.startswith("workflow run")]
    assert sent == (
        ["workflow run factory-status-pages.yml --repo acme/widgets --ref main"]
        if dispatched
        else []
    )
