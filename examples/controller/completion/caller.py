"""Thin public planning caller; the captured fixture grants no merge authority."""

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
                "controller-completion-plan",
                "--view",
                str(root / "view.json"),
                "--request",
                str(root / "request.json"),
            ],
            check=False,
        ).returncode
    )
