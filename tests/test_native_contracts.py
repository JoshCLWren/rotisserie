"""Native paths keep the operator-facing Python contracts."""

from __future__ import annotations

import importlib
import math
import statistics
from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction
from types import ModuleType
from typing import Any, cast

import pytest

from rotisserie.factory_console.runtime import module
from rotisserie.factory_console.service import checks, estimate_model


def policy() -> ModuleType:
    return cast(ModuleType, module("factory_work_policy_base").eligibility)


def test_native_policy_handles_cpython_character_classes() -> None:
    eligibility = policy()
    assert eligibility.explicit_closing_issue("πclose #8\nFıxeſ #٣") == 3
    assert eligibility.explicit_closing_issue("not close #0003\nCloses #٣") is None
    assert eligibility.explicit_closing_issue("Closes #123456789012345678901234567890") == (
        123456789012345678901234567890
    )
    assert eligibility.explicit_closing_issue("xdoes not close #3\ncloses #3") is None
    assert eligibility.parse_declared_dependencies(
        "Depends\x1con #٠٧ and [#８](link)\x85## Prerequısıtes\u2028- *`*#9**\r\n٢) #١٠"
    ) == {7, 8, 9, 10}
    assert eligibility.is_manual_only("café\n```text\nmanual-only\n```\n") is False
    assert eligibility.is_manual_only("π\nman`ignored`ual-only\n") is True
    assert eligibility.strip_code_segments("x\ud800`ignore`\ue000") == "x\ud800\ue000"


def test_log_reports_keep_unicode_paths_and_python_slicing() -> None:
    assert checks.summarize_log("src/组件.ts(１,2): error TS1: 非法") == (
        "compile",
        "src/组件.ts(１,2): error TS1: 非法",
        ("src/组件.ts",),
    )
    assert checks.summarize_log("\x1b[31m::error::\ud800" + "😀" * 202 + "\x1b[0m") == (
        "other",
        "\ud800" + "😀" * 199,
        (),
    )
    assert checks.summarize_log("Diff in /work/org/repo/\ud800.rs:1:\nDiff in \ue000.rs:1:") == (
        "formatting",
        "cargo fmt --check wants changes in \ud800.rs, \ue000.rs",
        ("\ud800.rs", "\ue000.rs"),
    )


@pytest.mark.parametrize(
    "values",
    [
        list(range(9)),
        [10**100 + i for i in range(8)],
        [Decimal(i) / 10 for i in range(11)],
        [Fraction(i, 7) for i in range(9)],
        (-math.inf, -0.0, 0.0, 0.0, -0.0, math.inf),
        [math.nan, 2.0, 9.0, 4.0, 5.0, 6.0],
        range(9),
    ],
)
def test_estimate_spread_preserves_python_numeric_results(values: Sequence[Any]) -> None:
    low, *_, high = statistics.quantiles(values, n=10, method="inclusive")
    expected = {"median": statistics.median(values), "low": low, "high": high, "n": len(values)}
    result = estimate_model.spread(values)
    assert result is not None
    assert list(result) == list(expected)
    for key, value in expected.items():
        actual = result[key]
        assert type(actual) is type(value)
        if isinstance(value, float) and math.isnan(value):
            assert isinstance(actual, float) and math.isnan(actual)
        else:
            assert actual == value
            if isinstance(value, float):
                assert isinstance(actual, float)
                assert math.copysign(1, actual) == math.copysign(1, value)


def test_native_preserves_python_errors() -> None:
    with pytest.raises(TypeError, match="cannot use a string pattern on a bytes-like object"):
        checks.summarize_log(cast(str, b"not a string"))
    with pytest.raises(TypeError, match="expected string or bytes-like object"):
        policy().explicit_closing_issue(42)
    with pytest.raises(OverflowError):
        estimate_model.spread([10**400] * 6)
    assert estimate_model.spread([1.0] * 5) is None


def test_existing_entrypoints_call_the_native_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    extension = importlib.import_module("_rotisserie_native")
    eligibility = policy()
    monkeypatch.setattr(extension, "is_manual_only", lambda _: True)
    monkeypatch.setattr(extension, "parse_declared_dependencies", lambda _: {101})
    monkeypatch.setattr(extension, "explicit_closing_issue", lambda _: 102)
    monkeypatch.setattr(extension, "summarize_log", lambda _: ("other", "native", ()))
    monkeypatch.setattr(extension, "spread", lambda _, minimum: {"n": minimum})
    assert eligibility.is_manual_only("no directive")
    assert eligibility.parse_declared_dependencies("") == {101}
    assert eligibility.explicit_closing_issue("") == 102
    assert checks.summarize_log("") == ("other", "native", ())
    assert estimate_model.spread([]) == {"n": estimate_model.MIN_SIMILAR}
