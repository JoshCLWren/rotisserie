"""Thin public CLI caller over one captured review view; starts no worker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    raise SystemExit(
        subprocess.run(
            [
                sys.executable,
                "-m",
                "rotisserie",
                "--config",
                str(root / "config.toml"),
                "controller-review-plan",
                "--view",
                str(root / "view.json"),
                "--assignment",
                str(root / "assignment.json"),
                "--operation",
                "demo-review-1",
                "--lease-id",
                "demo-review-lease-1",
                "--at",
                "10",
                "--max-observation-age",
                "0",
            ],
            check=False,
        ).returncode
    )
