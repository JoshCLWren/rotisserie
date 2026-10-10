"""Use only the installed public CLI; no host, worker, or mutation access."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE_SHA = "8692c718458282af9300edffce72035145ff6655"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "config.toml"
        config.write_bytes((ROOT / "config.toml").read_bytes())
        return subprocess.run(
            [
                "rotisserie",
                "--config",
                str(config),
                "controller-completion-compare",
                "--capture",
                str(ROOT / "capture.json"),
                "--capture-digest",
                hashlib.sha256((ROOT / "capture.json").read_bytes()).hexdigest(),
                "--source-sha",
                SOURCE_SHA,
                "--source-directory",
                str(ROOT / "source"),
            ],
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
