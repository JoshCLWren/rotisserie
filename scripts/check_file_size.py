#!/usr/bin/env python3
"""Lint: source files stay at or under LIMIT lines.

Files listed in the baseline predate the rule. They may only shrink, and the
baseline must be lowered when they do, so the list ratchets down to empty.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 500
BASELINE = ROOT / "tests" / "file_size_baseline.json"
PATTERNS = ("*.py", "*.cjs", "*.js", "*.sh")
EXCLUDED = ("prototype/", "reference/")


def sizes(root: Path = ROOT) -> dict[str, int]:
    listed = subprocess.run(
        ["git", "ls-files", *PATTERNS], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    return {
        path: len((root / path).read_text().splitlines())
        for path in listed
        if not path.startswith(EXCLUDED) and (root / path).is_file()
    }


def problems(found: Mapping[str, int], baseline: Mapping[str, int]) -> list[str]:
    out: list[str] = []
    for path, lines in sorted(found.items()):
        allowed = max(LIMIT, baseline.get(path, 0))
        if lines > allowed:
            if path in baseline:
                out.append(f"{path}: {lines} lines, grew past its baseline of {allowed}; split it")
            else:
                out.append(f"{path}: {lines} lines, over the {LIMIT}-line limit; split it")
    for path, recorded in sorted(baseline.items()):
        lines = found.get(path, 0)
        if lines <= LIMIT:
            out.append(f"{path}: now {lines} lines; remove it from {BASELINE.name}")
        elif lines < recorded:
            out.append(f"{path}: shrank to {lines} lines; lower its entry in {BASELINE.name}")
    return out


def main() -> int:
    baseline: dict[str, int] = json.loads(BASELINE.read_text())
    found = problems(sizes(), baseline)
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
