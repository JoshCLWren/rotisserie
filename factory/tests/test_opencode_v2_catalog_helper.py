"""The shared model catalog helper follows both OpenCode CLI interfaces."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_HELPER = ROOT / "scripts" / "opencode-model-catalog.sh"


def fake_cli(tmp_path: Path, version: str, expected_args: str) -> Path:
    """Create a model-catalog CLI double for either interface."""
    cli = tmp_path / "opencode"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "if [[ \"${1:-}\" == --version ]]; then\n"
        f"  echo '{version}'\n"
        "  exit 0\n"
        "fi\n"
        f"[[ \"$*\" == '{expected_args}' ]] || exit 9\n"
        "printf 'opencode/big-pickle\\n'\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return cli


def run_catalog(cli: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the shared catalog helper with a controlled executable."""
    env = os.environ.copy()
    env["OPENCODE_BIN"] = str(cli)
    return subprocess.run(
        ["bash", str(CATALOG_HELPER), "opencode", "verbose", "refresh"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_v2_lists_models_without_v1_provider_or_flags(tmp_path: Path) -> None:
    """OpenCode 2 uses the unfiltered standalone model list."""
    result = run_catalog(fake_cli(tmp_path, "opencode v2.0.32", "models --standalone"))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "opencode/big-pickle\n"


def test_v1_catalog_remains_compatible_with_local_tooling(tmp_path: Path) -> None:
    """Legacy local OpenCode 1 commands retain their existing flags."""
    result = run_catalog(
        fake_cli(tmp_path, "opencode v1.18.32", "models opencode --refresh --verbose")
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "opencode/big-pickle\n"
