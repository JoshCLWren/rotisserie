"""Source files stay small: new files under the limit, oversized files only shrink."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 500
BASELINE = ROOT / "tests" / "file_size_baseline.json"
PATTERNS = ("*.py", "*.cjs", "*.js", "*.sh")
EXCLUDED = ("prototype/", "reference/")


def _sizes() -> dict[str, int]:
    listed = subprocess.run(
        ["git", "ls-files", *PATTERNS], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return {
        path: len((ROOT / path).read_text().splitlines())
        for path in listed
        if not path.startswith(EXCLUDED) and (ROOT / path).is_file()
    }


def test_no_source_file_grows_past_its_limit() -> None:
    baseline: dict[str, int] = json.loads(BASELINE.read_text())
    too_big = {
        path: lines for path, lines in _sizes().items() if lines > max(LIMIT, baseline.get(path, 0))
    }
    assert not too_big, (
        f"Split these files: new files must stay at or under {LIMIT} lines and "
        f"files in {BASELINE.name} must not grow: {too_big}"
    )


def test_the_baseline_only_lists_files_that_are_still_oversized() -> None:
    baseline: dict[str, int] = json.loads(BASELINE.read_text())
    sizes = _sizes()
    stale = {
        path: sizes.get(path)
        for path, recorded in baseline.items()
        if sizes.get(path, 0) <= LIMIT or sizes[path] < recorded
    }
    assert not stale, f"Lower or remove these entries in {BASELINE.name}: {stale}"
