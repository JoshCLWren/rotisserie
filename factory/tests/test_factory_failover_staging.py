"""The trusted failover snapshot must be importable on its own.

Regression: stage_trusted_model_failover copied a hand-kept module list that
missed factory_model_catalog (imported by factory_live_opencode). Every
same-run failover crashed with ModuleNotFoundError and the worker gave up
after its first model failed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / ".github/scripts/free-model-factory-worker.sh"


def _stage_function() -> str:
    text = WORKER.read_text()
    start = text.index("stage_trusted_model_failover() {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _stage(tmp_path: Path) -> Path:
    script = _stage_function() + 'stage_trusted_model_failover\necho "$FACTORY_TRUSTED_FAILOVER_DIR"\n'
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return Path(result.stdout.strip().splitlines()[-1])


def test_every_staged_module_imports_from_the_isolated_snapshot(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    modules = sorted(p.stem for p in staged.glob("*.py"))
    assert "factory_model_failover" in modules
    assert "factory_model_catalog" in modules
    assert not [m for m in modules if m.startswith("test_")]

    # Same isolation as production: only the snapshot directory on sys.path.
    probe = (
        "import importlib, sys\n"
        "missing = []\n"
        "for name in sys.argv[1:]:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except ModuleNotFoundError as exc:\n"
        "        if (exc.name or '').startswith('factory'):\n"
        "            missing.append(f'{name}: {exc.name}')\n"
        "print('\\n'.join(missing))\n"
    )
    result = subprocess.run(
        ["python3", "-I", "-c", f"import sys; sys.path.insert(0, {str(staged)!r})\n{probe}", *modules],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.stdout.strip() == "", result.stdout


def test_failover_entrypoint_runs_from_snapshot(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    result = subprocess.run(
        ["python3", "-I", str(staged / "factory_model_failover.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "ModuleNotFoundError" not in result.stderr
    assert result.returncode == 0, result.stderr
