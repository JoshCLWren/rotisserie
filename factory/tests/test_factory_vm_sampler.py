"""Behavior of the factory VM sampler that survives hosted-runner deaths."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

SAMPLER = Path(__file__).resolve().parents[1] / ".github/scripts/factory-vm-sampler.sh"
SECRET_ARG = "sampler-secret-argument-7f3a"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _wait_for_ref(origin: Path, ref: str, deadline: float) -> str:
    while time.monotonic() < deadline:
        found = _git("--git-dir", str(origin), "for-each-ref", "--format=%(objectname)", ref)
        if found.strip():
            return found.strip()
        time.sleep(0.2)
    raise AssertionError(f"{ref} was never pushed")


def test_sampler_pushes_name_only_snapshots_and_deletes_ref_on_clean_stop(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.git"
    workspace = tmp_path / "ws"
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    _git("init", "--quiet", "--bare", str(origin))
    _git("init", "--quiet", str(workspace))
    _git("remote", "add", "origin", str(origin), cwd=workspace)

    # A process whose arguments must never be published from a public repo.
    decoy = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)", SECRET_ARG])
    env = {
        **os.environ,
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(runner_temp),
    }
    env.pop("GIT_DIR", None)
    sampler = subprocess.Popen(["bash", str(SAMPLER), "4242", "1"], env=env)
    ref = "refs/factory-diag/4242"
    try:
        _wait_for_ref(origin, ref, time.monotonic() + 15)
        snapshot = _git("--git-dir", str(origin), "show", f"{ref}:snapshot.txt")
        assert "--- memory (MiB)" in snapshot
        assert "--- top rss (KiB, name only)" in snapshot
        assert SECRET_ARG not in snapshot
    finally:
        sampler.send_signal(signal.SIGUSR1)
        assert sampler.wait(timeout=15) == 0
        decoy.kill()
        decoy.wait()

    remaining = _git("--git-dir", str(origin), "for-each-ref", ref)
    assert remaining == ""
    assert list(runner_temp.iterdir()) == []


def test_sampler_exits_quietly_without_an_origin(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _git("init", "--quiet", str(workspace))
    env = {**os.environ, "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(tmp_path)}
    result = subprocess.run(
        ["bash", str(SAMPLER), "1", "1"], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0
    assert result.stdout == ""
