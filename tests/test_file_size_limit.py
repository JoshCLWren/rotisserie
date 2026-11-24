"""The file size lint: new files under the limit, oversized files only shrink."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_file_size.py"
spec = importlib.util.spec_from_file_location("check_file_size", SCRIPT)
assert spec is not None and spec.loader is not None
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_a_new_file_over_the_limit_fails() -> None:
    assert check.problems({"a.py": 501}, {}) == [
        "a.py: 501 lines, over the 500-line limit; split it"
    ]
    assert check.problems({"a.py": 500}, {}) == []


def test_a_baseline_file_may_not_grow() -> None:
    assert check.problems({"big.py": 900}, {"big.py": 900}) == []
    assert check.problems({"big.py": 901}, {"big.py": 900}) == [
        "big.py: 901 lines, grew past its baseline of 900; split it"
    ]


def test_the_baseline_must_follow_a_shrink() -> None:
    assert check.problems({"big.py": 800}, {"big.py": 900}) == [
        "big.py: shrank to 800 lines; lower its entry in file_size_baseline.json"
    ]
    assert check.problems({"big.py": 300}, {"big.py": 900}) == [
        "big.py: now 300 lines; remove it from file_size_baseline.json"
    ]


def test_the_repository_passes_the_lint() -> None:
    assert check.main() == 0
