"""Thin public CLI caller over one captured recovery view; starts no worker."""

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
                "controller-recovery-plan",
                "--view",
                str(root / "recovery-view.json"),
                "--operation",
                "demo-recovery-1",
                "--at",
                "20",
                "--max-observation-age",
                "0",
                "--inactivity-ttl",
                "10",
            ],
            check=False,
        ).returncode
    )
