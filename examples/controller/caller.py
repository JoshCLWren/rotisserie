"""Thin public CLI caller over one captured host view; starts no worker."""

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
                "controller-plan",
                "--view",
                str(root / "view.json"),
                "--assignment",
                str(root / "assignment.json"),
                "--operation",
                "demo-tick-1",
                "--lease-id",
                "demo-lease-1",
                "--at",
                "10",
                "--max-observation-age",
                "0",
            ],
            check=False,
        ).returncode
    )
