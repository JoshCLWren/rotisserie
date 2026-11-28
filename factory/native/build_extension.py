"""Build the locked extension for both installed packages and factory checkouts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from tempfile import TemporaryDirectory
from pathlib import Path


def build() -> Path:
    manifest = Path(__file__).with_name("Cargo.toml").resolve()
    result = subprocess.run(
        [
            "cargo", "build", "--locked", "--release",
            "--manifest-path", str(manifest), "--message-format=json",
        ],
        cwd=manifest.parent,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    for line in result.stdout.splitlines():
        message = json.loads(line)
        if (
            message.get("reason") == "compiler-artifact"
            and message["target"]["name"] == "_rotisserie_native"
            and "cdylib" in message["target"]["crate_types"]
        ):
            for filename in message["filenames"]:
                artifact = Path(filename)
                if artifact.suffix in (".so", ".dylib", ".dll"):
                    return artifact
    raise RuntimeError("Cargo did not report a Rotisserie extension artifact")


def extension_filename() -> str:
    return "_rotisserie_native.pyd" if os.name == "nt" else "_rotisserie_native.abi3.so"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    artifact = build()
    filename = extension_filename()
    with TemporaryDirectory(dir=args.directory) as tempdir:
        staged = Path(tempdir) / filename
        shutil.copyfile(artifact, staged)
        staged.replace(args.directory / filename)


if __name__ == "__main__":
    main()
