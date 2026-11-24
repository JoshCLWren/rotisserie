"""Shell workers skip the adopter's [issues] reserved list when they choose an issue."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
WORKERS = (
    "free-model-factory-worker-primitives.sh",
    "nvidia-factory-worker.sh",
    "omniroute-factory-worker.sh",
)
FILTER = "map(select(.number as $n | $reserved | index($n) | not))"


def test_adopter_cli_prints_the_configured_reserved_issues() -> None:
    result = subprocess.run(
        ["python3", str(SCRIPTS / "factory_adopter.py"), "reserved-issues"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == [9991, 9992]


@pytest.mark.parametrize("worker", WORKERS)
def test_worker_issue_choice_drops_reserved_issues(worker: str) -> None:
    source = (SCRIPTS / worker).read_text(encoding="utf-8")
    assert 'factory_adopter.py" reserved-issues' in source
    assert FILTER in source
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq not installed")
    result = subprocess.run(
        [jq, "-c", "--argjson", "reserved", "[9991]", FILTER + " | map(.number)"],
        input='[{"number": 5}, {"number": 9991}, {"number": 7}]',
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == [5, 7]
