"""Release tooling fails closed before building modified source."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_dirty_source_is_rejected_without_artifacts(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    verifier = scripts / "verify-release.py"
    verifier.write_bytes((ROOT / "scripts/verify-release.py").read_bytes())
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    output = tmp_path / "candidate"
    result = subprocess.run(
        [sys.executable, str(verifier), "--output", str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "requires a clean worktree" in result.stderr
    assert not output.exists()
