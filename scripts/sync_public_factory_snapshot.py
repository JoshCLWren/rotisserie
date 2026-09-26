#!/usr/bin/env python3
"""Synchronize the allowlisted public factory snapshot from Comic Pile."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

FILE_MAP = {
    Path("AGENTS.md"): Path("AGENTS.md"),
    Path("docs/CHATGPT_FACTORY_PROMPT.md"): Path("FACTORY_PROMPT.md"),
    Path("docs/AUTONOMOUS_FACTORY_POLICY.md"): Path("docs/AUTONOMOUS_FACTORY_POLICY.md"),
    Path("docs/ISSUE_EXECUTION_PROTOCOL.md"): Path("docs/ISSUE_EXECUTION_PROTOCOL.md"),
    Path("docs/FACTORY_GITHUB_VISIBILITY.md"): Path("docs/FACTORY_GITHUB_VISIBILITY.md"),
}


def sync_snapshot(source_root: Path, target_root: Path, *, check: bool = False) -> list[Path]:
    """Copy or verify the explicitly allowlisted factory documentation.

    Args:
        source_root: Comic Pile repository root.
        target_root: Public factory snapshot repository root.
        check: Report drift without writing when true.

    Returns:
        Target-relative paths that differ from the source.
    """
    changed: list[Path] = []
    for source_relative, target_relative in FILE_MAP.items():
        source = source_root / source_relative
        target = target_root / target_relative
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Unsafe or missing source file: {source}")
        if target.is_symlink():
            raise ValueError(f"Refusing to replace target symlink: {target}")

        source_bytes = source.read_bytes()
        target_bytes = target.read_bytes() if target.is_file() else None
        if source_bytes == target_bytes:
            continue
        changed.append(target_relative)
        if not check:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    return changed


def main() -> None:
    """Parse command-line arguments and synchronize the snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    changed = sync_snapshot(args.source.resolve(), args.target.resolve(), check=args.check)
    for path in changed:
        print(path)
    if args.check and changed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
