"""Stacked factory gh shims must reach the real gh, never recurse.

Regression: the session step put the usage meter first on PATH, then the
worker's REST-shim installer re-resolved ``command -v gh`` and exported the
meter itself as FACTORY_REAL_GH. Every gh call recursed until the hosted VM
ran out of processes (~70 forks/s, runner shut down ~4 minutes in).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"


def _stack(tmp_path: Path) -> tuple[dict[str, str], Path]:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "gh"
    real.write_text('#!/usr/bin/env bash\necho "REAL-GH depth=${FACTORY_GH_SHIM_DEPTH:-0} $*"\n')
    real.chmod(0o755)

    # Session step: meter copied as `gh`, real gh resolved before PATH moves.
    meter_dir = tmp_path / "meter"
    meter_dir.mkdir()
    shutil.copy(SCRIPTS / "factory-gh-meter.sh", meter_dir / "gh")
    (meter_dir / "gh").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{meter_dir}:{real_dir}:{os.environ['PATH']}",
        "FACTORY_REAL_GH": str(real),
        "FACTORY_GH_CALL_LOG": str(tmp_path / "calls.log"),
        "GITHUB_WORKSPACE": str(ROOT),
    }
    for key in ("FACTORY_READ_TOKEN", "FACTORY_GH_SHIM_DEPTH", "FACTORY_DISCOVERY_DIR"):
        env.pop(key, None)
    return env, real


def _worker_install_function() -> str:
    worker = (SCRIPTS / "free-model-factory-worker.sh").read_text()
    start = worker.index("install_factory_rest_gh() {")
    end = worker.index("\n}\n", start) + 3
    return worker[start:end]


def test_worker_rest_shim_keeps_outer_real_gh_and_calls_it_once(tmp_path: Path) -> None:
    env, real = _stack(tmp_path)
    script = (
        _worker_install_function()
        + 'install_factory_rest_gh\necho "resolved=$FACTORY_REAL_GH"\ngh auth status\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert f"resolved={real}" in result.stdout
    assert "REAL-GH depth=1 auth status" in result.stdout


def test_meter_pointed_at_itself_falls_through_to_real_gh(tmp_path: Path) -> None:
    env = _stack(tmp_path)[0]
    # The exact broken state: FACTORY_REAL_GH names the meter shim itself.
    env["FACTORY_REAL_GH"] = str(tmp_path / "meter" / "gh")
    result = subprocess.run(
        ["gh", "api", "repos/o/r/pulls/1"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert result.stdout.strip() == "REAL-GH depth=1 api repos/o/r/pulls/1"


def test_meter_refuses_runaway_depth_instead_of_forking(tmp_path: Path) -> None:
    env = _stack(tmp_path)[0]
    env["FACTORY_GH_SHIM_DEPTH"] = "5"
    result = subprocess.run(
        ["gh", "pr", "view", "1"], env=env, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 125
    assert "recursion detected" in result.stderr
    assert "REAL-GH" not in result.stdout
