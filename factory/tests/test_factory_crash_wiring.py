"""Every factory entry point installs crash capture, and the bash ERR trap records shell crashes."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

FACTORY = Path(__file__).resolve().parents[1]
ROOT = FACTORY.parent
SCRIPTS = FACTORY / ".github" / "scripts"
WORKFLOWS = [*(ROOT / ".github" / "workflows").glob("*.yml")]
ACTIONS = [*(FACTORY / ".github" / "actions").glob("*/action.y*ml")]
REFERENCE = re.compile(r"scripts/([A-Za-z0-9_.-]+\.py)\b")
SHELL_REFERENCE = re.compile(r"\b([A-Za-z0-9_-]+\.py)\b")
MAIN = re.compile(r"^if __name__ == ['\"]__main__['\"]:\n(?P<body>(?:(?:    .*)?\n)+)", re.M)
HOOKED = re.compile(r"factory_crash(?:\.run\(|\"\)\.install\(\))")


def entry_points() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for source in [*WORKFLOWS, *ACTIONS]:
        for name in REFERENCE.findall(source.read_text(encoding="utf-8")):
            for folder in (SCRIPTS, FACTORY / "scripts"):
                if (folder / name).is_file():
                    found[name] = folder / name
    for shell in SCRIPTS.glob("*.sh"):
        for name in SHELL_REFERENCE.findall(shell.read_text(encoding="utf-8")):
            if (SCRIPTS / name).is_file():
                found[name] = SCRIPTS / name
    found.pop("factory_crash.py", None)
    return {name: path for name, path in found.items() if "__main__" in path.read_text()}


def test_every_python_entry_point_installs_crash_capture_in_its_main_block() -> None:
    points = entry_points()
    assert {"factory-work-controller.py", "factory_self_heal.py", "fixed-model-guard.py"} <= set(
        points
    )
    assert len(points) >= 55
    missing = []
    for name, path in sorted(points.items()):
        block = MAIN.search(path.read_text(encoding="utf-8"))
        if block is None or not HOOKED.search(block.group("body")):
            missing.append(name)
    assert not missing, f"install factory_crash in the __main__ block of: {missing}"


def test_the_runtime_overlay_and_the_fork_helpers_ship_the_crash_modules() -> None:
    overlay = (FACTORY / "install" / "overlay-paths.txt").read_text().split()
    for name in ("factory_crash.py", "factory_crash_store.py", "factory_crash_trap.sh"):
        assert f".github/scripts/{name}" in overlay
    fork = (ROOT / ".github" / "workflows" / "factory-fork-execution.yml").read_text()
    assert ".github/scripts/factory_crash.py" in fork


def test_the_worker_and_checkpoint_shell_scripts_install_the_err_trap() -> None:
    for name in ("free-model-factory-worker.sh", "factory-work-checkpoint.sh"):
        assert "factory_crash_trap.sh" in (SCRIPTS / name).read_text(), name


def test_the_status_pages_job_publishes_crash_groups_every_run() -> None:
    pages = (ROOT / ".github" / "workflows" / "factory-status-pages.yml").read_text()
    assert "factory_crash.py export" in pages and ".crashes = $crashes[0]" in pages


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_failing_shell_command_is_annotated_stored_and_keeps_its_status(
    tmp_path: Path,
) -> None:
    script = tmp_path / "worker.sh"
    script.write_text(
        "set -Eeuo pipefail\n"
        f"source {SCRIPTS / 'factory_crash_trap.sh'}\n"
        "false || true\n"
        "if grep -q nothing /dev/null; then :; fi\n"
        "step() {\n"
        '  value="$(grep -c needle /dev/null)"\n'
        "}\n"
        "step\n"
        "echo never\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_TURSO", "GITHUB_"))}
    store = tmp_path / "store.db"
    env.update({"FACTORY_STATE_SQLITE": str(store), "GITHUB_REPOSITORY": "acme/widgets"})
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, check=False, timeout=60
    )
    assert result.returncode == 1
    assert "never" not in result.stdout
    assert "::error file=" in result.stderr and "title=Factory shell crash::" in result.stderr
    assert "worker.sh:6 step" in result.stderr
    conn = sqlite3.connect(store)
    try:
        rows = list(conn.execute("SELECT kind, script, function, line, message FROM factory_crash"))
    finally:
        conn.close()
    assert len(rows) == 1
    kind, name, function, line, message = rows[0]
    assert (kind, name, function, line) == ("shell", "worker.sh", "step", 6)
    assert "grep -c needle /dev/null" in message and "exited 1" in message


def test_an_entry_point_staged_without_its_siblings_still_runs(tmp_path: Path) -> None:
    alone = tmp_path / "factory_snapshot_age.py"
    shutil.copy2(SCRIPTS / alone.name, alone)
    result = subprocess.run(
        ["python3", "-I", str(alone), "2026-11-25T16:00:00Z"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().isdigit()


def _failing(tmp_path: Path, helper: Path) -> Path:
    script = tmp_path / "step.sh"
    script.write_text(f"set -Eeuo pipefail\nsource {helper}\nfalse\n")
    return script


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_without_a_store_the_trap_only_annotates_and_runs_no_recorder(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    shutil.copy2(SCRIPTS / "factory_crash_trap.sh", staged / "factory_crash_trap.sh")
    marker = tmp_path / "recorder-ran"
    (staged / "factory_crash.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_TURSO", "FACTORY_STATE"))}
    script = _failing(tmp_path, staged / "factory_crash_trap.sh")
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 1 and "title=Factory shell crash::false exited 1" in result.stderr
    assert not marker.exists()
    env["FACTORY_STATE_SQLITE"] = str(tmp_path / "store.db")
    subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, check=False)
    assert marker.exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_the_checkpoint_writer_staged_alone_still_starts(tmp_path: Path) -> None:
    staged = tmp_path / "factory-work-checkpoint.abc123"
    shutil.copy2(SCRIPTS / "factory-work-checkpoint.sh", staged)
    result = subprocess.run(["bash", str(staged)], capture_output=True, text=True, check=False)
    assert result.returncode == 2 and "usage: factory-work-checkpoint.sh" in result.stderr
    assert "No such file" not in result.stderr
