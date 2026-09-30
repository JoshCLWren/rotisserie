#!/usr/bin/env python3
"""Build twice from committed source and verify a wheel rebuilt from the sdist."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()


def digests(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.name.endswith((".whl", ".tar.gz"))
    }


def verify(output: Path) -> None:
    if run("git", "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("release verification requires a clean worktree")
    revision = run("git", "rev-parse", "HEAD")
    epoch = run("git", "show", "-s", "--format=%ct", "HEAD")
    environment = dict(os.environ, SOURCE_DATE_EPOCH=epoch, PYTHONHASHSEED="0")
    # Remove index and credential configuration from durable evidence; record only versions.
    with tempfile.TemporaryDirectory(prefix="rotisserie-release-") as temporary:
        workspace = Path(temporary)
        archive = workspace / "source.tar"
        subprocess.run(
            ["git", "archive", "--format=tar", f"--output={archive}", revision],
            cwd=ROOT,
            check=True,
        )
        builds: list[Path] = []
        for number in range(2):
            source = workspace / f"source-{number}"
            source.mkdir()
            with tarfile.open(archive) as bundle:
                bundle.extractall(source, filter="data")
            destination = workspace / f"dist-{number}"
            run(
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(destination),
                str(source),
                env=environment,
            )
            builds.append(destination)
        expected = digests(builds[0])
        if len(expected) != 2 or not any(name.endswith(".whl") for name in expected):
            raise ValueError("expected exactly one wheel and one source distribution")
        if expected != digests(builds[1]):
            raise ValueError("independent builds differ")
        sdist = next(builds[0].glob("*.tar.gz"))
        unpacked = workspace / "sdist"
        unpacked.mkdir()
        with tarfile.open(sdist) as bundle:
            bundle.extractall(unpacked, filter="data")
        rebuilt = workspace / "rebuilt"
        run(
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--outdir",
            str(rebuilt),
            str(next(unpacked.iterdir())),
            env=environment,
        )
        wheels = {name: digest for name, digest in expected.items() if name.endswith(".whl")}
        if digests(rebuilt) != wheels:
            raise ValueError("wheel rebuilt from source distribution differs")
        # Reserve a new directory only after all verification succeeds.
        output.mkdir(parents=True, exist_ok=False)
        for name in expected:
            (output / name).write_bytes((builds[0] / name).read_bytes())
        (output / "SHA256SUMS").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in expected.items()), encoding="utf-8"
        )
        evidence = {
            "schema_version": 1,
            "source_revision": revision,
            "source_date_epoch": int(epoch),
            "python": sys.version.split()[0],
            "uv": run("uv", "--version"),
            "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
            "artifacts": expected,
            "independent_builds_match": True,
            "sdist_wheel_matches": True,
            "signed": False,
        }
        (output / "build-evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(f"Verified release artifacts: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new artifact directory")
    args = parser.parse_args()
    try:
        verify(args.output.resolve())
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release verification failed: {error}\n")


if __name__ == "__main__":
    main()
