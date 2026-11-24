#!/usr/bin/env python3
"""Install Rotisserie factory caller workflows into an adopter repository.

Each file under factory/install/callers is a thin caller: the adopter's triggers,
permissions, concurrency and run-name, plus one job that calls the reusable
workflow of the same name in Rotisserie's .github/workflows at --ref (main by
default, or a pinned commit). The reusable workflow checks out Rotisserie at the
same commit and overlays the factory runtime, so no factory code lands in the
adopter.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CALLERS = Path(__file__).resolve().parent / "callers"
DEFAULT_SOURCE = "JoshCLWren/rotisserie"
DEFAULT_REF = "main"
# Installed by earlier Rotisserie versions; the reusable workflows replace them.
RETIRED = frozenset({"free-model-factory-run.yml"})
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}\Z")
SOURCE_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+\Z")
USES_RE = re.compile(
    rf"^(?P<indent>\s*)uses: {re.escape(DEFAULT_SOURCE)}/\.github/workflows/"
    rf"(?P<name>[a-z0-9-]+\.yml)@{DEFAULT_REF}\s*$",
    re.MULTILINE,
)


class InstallError(ValueError):
    pass


def render(caller: str, *, source: str, ref: str) -> str:
    if not (SHA_RE.fullmatch(ref) or (BRANCH_RE.fullmatch(ref) and ".." not in ref)):
        raise InstallError("ref must be a branch name or a full 40-character commit SHA")
    if not SOURCE_RE.fullmatch(source):
        raise InstallError("source must be owner/name")
    rendered, count = USES_RE.subn(
        lambda match: f"{match['indent']}uses: {source}/.github/workflows/{match['name']}@{ref}",
        caller,
    )
    if count != 1:
        raise InstallError("a caller must call exactly one Rotisserie reusable workflow")
    return rendered


def install(
    target: Path, *, source: str, ref: str, names: list[str] | None = None
) -> tuple[list[Path], list[Path]]:
    workflows = target / ".github" / "workflows"
    if not (target / ".git").exists():
        raise InstallError(f"{target} is not a git checkout")
    callers = sorted(CALLERS.glob("*.yml"))
    if names:
        wanted = set(names)
        unknown = wanted - {path.name for path in callers}
        if unknown:
            raise InstallError(f"unknown workflows: {', '.join(sorted(unknown))}")
        callers = [path for path in callers if path.name in wanted]
    workflows.mkdir(parents=True, exist_ok=True)
    written = []
    for caller in callers:
        destination = workflows / caller.name
        destination.write_text(
            render(caller.read_text(encoding="utf-8"), source=source, ref=ref), encoding="utf-8"
        )
        written.append(destination)
    removed = []
    if not names:
        for name in sorted(RETIRED):
            stale = workflows / name
            if stale.exists():
                stale.unlink()
                removed.append(stale)
    return written, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="adopter repository checkout")
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help="Rotisserie branch to track (default: main) or a commit SHA to pin",
    )
    parser.add_argument(
        "--source", default=DEFAULT_SOURCE, help="Rotisserie repository (owner/name)"
    )
    parser.add_argument(
        "--workflow", action="append", dest="workflows", help="install only this caller"
    )
    args = parser.parse_args(argv)
    try:
        written, removed = install(
            args.target.resolve(), source=args.source, ref=args.ref, names=args.workflows
        )
    except InstallError as exc:
        print(f"install: {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(path)
    for path in removed:
        print(f"removed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
