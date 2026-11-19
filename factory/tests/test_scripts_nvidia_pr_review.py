"""Focused tests for the NVIDIA pull-request review benchmark harness."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_script_module() -> ModuleType:
    """Load the benchmark script as a testable module."""
    path = Path(__file__).parent.parent / "scripts" / "test_nvidia_pr_review.py"
    spec = importlib.util.spec_from_file_location("nvidia_pr_review_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


review = _load_script_module()


def _finding(line: int = 219, body: str = "Timezone formatting uses the wrong boundary") -> object:
    """Build one representative validated finding."""
    return review.Finding(
        path="frontend/src/pages/WhatsNewPage.tsx",
        line=line,
        body=body,
    )


def _result(
    *,
    status: str = "completed",
    classification: str = "passed",
    verdict: str | None = "CHANGES_REQUIRED",
    findings: tuple[object, ...] | None = None,
) -> object:
    """Build a model result for quality-scoring tests."""
    return review.ModelResult(
        model="nvidia/example",
        status=status,
        verdict=verdict,
        findings=findings if findings is not None else (_finding(),),
        elapsed_seconds=1.0,
        detail="test",
        output_dir=Path("results"),
        classification=classification,
    )


def _tool_call(name: str, arguments: dict[str, object], call_id: str = "call-1") -> dict[str, object]:
    """Build one OpenAI-compatible tool call."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_edit_conversation_has_no_user_message_after_tool_result() -> None:
    """Mistral receives assistant tool-call -> tool result -> assistant sequencing."""
    initial = review._initial_messages("review this diff")
    first_message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "private provider reasoning",
        "tool_calls": [_tool_call("write_review_file", {"review": {}})],
    }

    messages = review._edit_messages(initial, first_message, "call-1")

    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool"]
    assert messages[-1]["role"] == "tool"
    assert messages[-2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": first_message["tool_calls"],
    }
    assert "reasoning_content" not in messages[-2]


def test_reasoning_fields_are_not_treated_as_final_review_content() -> None:
    """Null content stays null even when provider reasoning fields contain text."""
    message = {
        "content": None,
        "reasoning": "analysis",
        "reasoning_content": '{"verdict":"PASS","findings":[]}',
    }

    assert review._assistant_text(message) is None
    assert review._response_metadata(message) == {
        "content_present": False,
        "reasoning_present": True,
        "reasoning_content_present": True,
    }


def test_tool_arguments_accept_provider_object_arguments() -> None:
    """Providers may return already-decoded tool arguments instead of JSON text."""
    message = {
        "tool_calls": [
            _tool_call(
                "write_review_file",
                {"review": {"verdict": "PASS", "findings": []}},
            ),
        ],
    }

    call_id, arguments = review._tool_arguments(message, "write_review_file")

    assert call_id == "call-1"
    assert arguments["review"] == {"verdict": "PASS", "findings": []}


def test_known_defect_line_range_survives_line_drift() -> None:
    """A moved line still matches a deliberately tolerant expected range."""
    expected = review._parse_expected_locations(
        ["frontend/src/pages/WhatsNewPage.tsx:210-230"],
    )

    assert review._review_quality(_result(), expected) == "known_defects_found"


def test_known_defect_text_pattern_survives_line_drift() -> None:
    """Finding text can identify a known defect without pinning an exact line."""
    expected = review._parse_expected_text(
        ["frontend/src/pages/WhatsNewPage.tsx:timezone|local time"],
    )
    moved_result = _result(findings=(_finding(line=260, body="Local time uses a timezone boundary incorrectly"),))

    assert review._review_quality(moved_result, expected) == "known_defects_found"


def test_review_quality_is_independent_from_tool_compatibility() -> None:
    """A useful review remains useful even when the tool protocol later fails."""
    expected = review._parse_expected_locations(
        ["frontend/src/pages/WhatsNewPage.tsx:210-230"],
    )
    failed_tool_result = _result(status="failed", classification="tool_call_not_emitted")

    assert review._review_quality(failed_tool_result, expected) == "known_defects_found"


def test_tool_compatible_false_pass_is_scored_as_missed_defect() -> None:
    """Successful tool calls do not rescue a PASS that misses the known defect."""
    expected = review._parse_expected_locations(
        ["frontend/src/pages/WhatsNewPage.tsx:210-230"],
    )
    false_pass = _result(verdict="PASS", findings=())

    assert review._review_quality(false_pass, expected) == "missed_known_defect"


def test_initial_tool_review_survives_second_turn_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A valid first tool review remains scoreable when the edit tool is not emitted."""
    finding = {
        "path": "frontend/src/pages/WhatsNewPage.tsx",
        "line": 219,
        "body": "Timezone formatting can cross the viewer's local day boundary.",
    }
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "reviewed the timezone behavior",
                            "tool_calls": [
                                _tool_call(
                                    "write_review_file",
                                    {
                                        "review": {
                                            "verdict": "CHANGES_REQUIRED",
                                            "findings": [finding],
                                        },
                                    },
                                ),
                            ],
                        },
                    },
                ],
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "The original review is still correct.",
                        },
                    },
                ],
            },
        ],
    )

    def fake_request(*_args: object, **_kwargs: object) -> object:
        """Return deterministic provider responses for both turns."""
        return next(responses)

    monkeypatch.setattr(review, "_request_nim", fake_request)

    result = review._review_model(
        "nvidia/example",
        "secret",
        "prompt",
        {"frontend/src/pages/WhatsNewPage.tsx": {219}},
        30,
        tmp_path,
    )

    assert result.status == "failed"
    assert result.classification == "tool_call_not_emitted"
    assert result.verdict == "CHANGES_REQUIRED"
    assert result.findings == (_finding(body=finding["body"]),)
    expected = review._parse_expected_locations(
        ["frontend/src/pages/WhatsNewPage.tsx:210-230"],
    )
    assert review._review_quality(result, expected) == "known_defects_found"
