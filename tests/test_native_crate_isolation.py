"""The native crate builds when an adopter checks Rotisserie out inside its own Cargo workspace."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NATIVE = Path(__file__).resolve().parents[1] / "factory" / "native"


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_native_crate_stays_out_of_an_adopter_workspace(tmp_path: Path) -> None:
    # Adopters such as comic-pile overlay Rotisserie at .rotisserie/runtime and
    # have their own workspace Cargo.toml at the repository root.
    (tmp_path / "Cargo.toml").write_text("[workspace]\nmembers = []\n", encoding="utf-8")
    crate = tmp_path / ".rotisserie" / "runtime" / "factory" / "native"
    crate.mkdir(parents=True)
    for name in ("Cargo.toml", "Cargo.lock"):
        shutil.copy2(NATIVE / name, crate / name)
    shutil.copytree(NATIVE / "src", crate / "src")

    result = subprocess.run(
        [
            "cargo",
            "metadata",
            "--no-deps",
            "--format-version=1",
            "--manifest-path",
            str(crate / "Cargo.toml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"workspace_root":"' + str(crate) + '"' in result.stdout
