"""Regression tests for exact-head issue-contract verification (#4045)."""

import importlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
acceptance = importlib.import_module("factory_issue_acceptance")
HEAD = "a" * 40
NEXT = "b" * 40
ISSUE = "## Scope\n- Render folders and pagination\n- Show source preview\n\n## Acceptance tests\n- Browser test proves navigation\n"
FILE = "frontend/src/pages/CblBrowserPage.tsx"


def complete_report():
    return {
        "issue": 4010,
        "head": HEAD,
        "issue_digest": acceptance.digest(ISSUE),
        "items": [
            {
                "id": f"A{i}",
                "status": "pass",
                "file": FILE,
                "evidence": "Verified the actual browser and source preview implementation in this changed file.",
                "test": "pnpm playwright test cbl-browser.spec.ts passed",
            }
            for i in range(1, 4)
        ],
    }


def errors(report=None, body=ISSUE, head=HEAD, paths=(FILE,)):
    return acceptance.validate(
        complete_report() if report is None else report, 4010, head, body, paths
    )


def test_complete_issue_requirements_pass():
    assert len(acceptance.requirements(ISSUE)) == 3
    assert errors() == []


def test_missing_criterion_blocks_approval():
    report = complete_report()
    report["items"].pop()
    assert "cover all 3" in " ".join(errors(report))


def test_false_pass_without_actual_changed_file_blocks():
    report = complete_report()
    report["items"][0]["file"] = "docs/issue-plans/4010.md"
    assert "cite a file changed" in " ".join(errors(report))


def test_generic_claim_without_test_or_evidence_blocks():
    report = complete_report()
    report["items"][0]["evidence"] = "done"
    report["items"][0]["test"] = "tests passed"
    assert len(errors(report)) == 2


def test_malformed_or_duplicate_report_fails_closed():
    report = complete_report()
    report["items"][1]["id"] = "A1"
    assert errors(report)
    assert acceptance.report_from_text("FACTORY_GATE_READY") is None
    assert acceptance.report_from_text("FACTORY_ACCEPTANCE_V1=not-json") is None


def test_contract_head_and_issue_must_match():
    assert errors(head=NEXT)
    assert errors(body=ISSUE + "\n- another requirement")


def test_issue_body_revision_and_head_invalidate_trusted_marker():
    old = acceptance.marker(4010, HEAD, ISSUE)
    assert acceptance.attested([old], 4010, HEAD, ISSUE)
    assert not acceptance.attested([f"{old}\n### Review"], 4010, HEAD, ISSUE)
    assert not acceptance.attested([old], 4010, NEXT, ISSUE)
    assert not acceptance.attested([old], 4010, HEAD, ISSUE + "\nNew requirement")
    assert not acceptance.attested(["<!-- untrusted imitation -->"], 4010, HEAD, ISSUE)
    assert not acceptance.attested([f"### rejected review\n```text\n{old}\n```"], 4010, HEAD, ISSUE)


def test_doc_only_issue_is_not_forced_to_change_executable_files():
    issue = "## Scope\n- Correct the contributor guide\n"
    report = {
        "issue": 4010, "head": HEAD, "issue_digest": acceptance.digest(issue), "items": [
            {"id": "A1", "status": "pass", "file": "docs/guide.md",
             "evidence": "Updated actual instructions, verified link and command names by inspection.",
             "test": "manual docs inspection: all examples checked"}
        ],
    }
    assert errors(report, body=issue, paths=("docs/guide.md",)) == []


def test_docstring_only_pr_cannot_claim_unmodified_executable_code():
    report = complete_report()
    report["items"][0]["file"] = "app/services/reading_list_cycle_validator.py"
    # A separate #4042 mechanical-diff gate rejects a docstring-only change.
    # Here it is also impossible to claim the browser implementation from
    # a changed file that does not implement any browser behavior.
    assert errors(report, paths=("app/services/reading_list_cycle_validator.py",))


def test_report_line_is_structured_and_exact():
    import json
    report = complete_report()
    line = acceptance.PREFIX + json.dumps(report)
    assert acceptance.report_from_text("Read issue and diff\n" + line + "\nFACTORY_GATE_READY") == report
    assert acceptance.report_from_text(line + "\n" + line) is None


def test_new_completion_criteria_and_prose_are_not_discarded():
    issue = (
        "## Scope\n- First executable requirement\n  with additional details\n"
        "A standalone paragraph also establishes behavior.\n"
        "## Completion criteria\n- New section must not be skipped\n"
    )
    entries = acceptance.requirements(issue)
    assert any("additional details" in text for text in entries)
    assert any("standalone paragraph" in text for text in entries)
    assert any("New section" in text for text in entries)


def test_report_cannot_approve_an_issue_edit_with_same_number_of_criteria():
    before = "## Scope\n- Save the user setting\n"
    after = "## Scope\n- Delete all user settings\n"
    report = {
        "issue": 4010,
        "head": HEAD,
        "issue_digest": acceptance.digest(before),
        "items": [{"id": "A1", "status": "pass", "file": FILE,
                   "evidence": "Verified the save operation and its invariants in the code.",
                   "test": "pytest tests/test_settings.py passed"}],
    }
    assert "changed during review" in " ".join(errors(report, body=after))


def test_supported_link_spellings_have_same_issue_identity():
    for closing in ("Closes #4010", "Fixes #4010", "Resolves #4010", "close #4010"):
        assert acceptance.linked_issue("manual/review", closing) == 4010
    assert acceptance.linked_issue("factory/54-4010-cbl", "Closes #999") == 4010


def test_part_of_link_is_review_only_and_crlf_safe():
    assert acceptance.linked_issue_mode(
        "local/acp-4348", "Part of #4348.\r\n"
    ) == (4348, True)
    assert acceptance.linked_issue_mode(
        "local/acp-4348", "Part of #4348 and governed by #4335."
    ) == (4348, True)
    assert acceptance.linked_issue_mode(
        "local/acp-4348", "Closes #4010.\nPart of #4348."
    ) == (4010, False)
    assert acceptance.linked_issue_mode(
        "factory/54-4010-cbl", "Part of #4348."
    ) == (4010, False)
    assert acceptance.linked_issue_mode(
        "factory/75-4740-opencode-free", "Part of #4740."
    ) == (4740, True)
    assert acceptance.linked_issue_mode(
        "factory/75-4740-opencode-free", "Part of #4740 and governed by #4335."
    ) == (4740, True)
    assert acceptance.linked_issue_mode(
        "factory/75-4740-opencode-free", "Closes #4740.\nPart of #4740."
    ) == (4740, False)
    assert acceptance.linked_issue_mode("local/x", "Advances #4348.") == (None, False)
    assert acceptance.linked_issue_mode("local/x", "- Part of #4348.") == (None, False)


def test_non_closing_acceptance_allows_remaining_but_closing_does_not():
    report = complete_report()
    report["items"][2] = {
        "id": "A3",
        "status": "remaining",
        "evidence": (
            "Production proof and cleanup remain on the linked issue after this "
            "review-only handoff."
        ),
    }
    assert any("missing PASS evidence" in error for error in errors(report))
    assert acceptance.validate(
        report, 4010, HEAD, ISSUE, (FILE,), non_closing=True
    ) == []


def test_large_report_parses_without_truncating_criterion_evidence():
    import json
    report = complete_report()
    report["items"][0]["evidence"] = "Specific implementation evidence " * 400
    line = acceptance.PREFIX + json.dumps(report)
    assert len(line) > 7000
    assert acceptance.report_from_text("Review complete\n" + line) == report


def test_opencode_jsonl_assistant_text_acceptance_report_is_parsed():
    """A real OpenCode assistant text event can carry the structured report."""
    import json

    report = complete_report()
    line = acceptance.PREFIX + json.dumps(report)
    event = json.dumps({
        "type": "text",
        "timestamp": 1793748011648,
        "part": {"type": "text", "text": line + "\nFACTORY_GATE_READY"},
    })
    assert acceptance.report_from_text(event) == report
    assert acceptance.validate(
        acceptance.report_from_text(event), 4010, HEAD, ISSUE, (FILE,)
    ) == []


def test_tool_output_and_event_metadata_cannot_mint_acceptance():
    """Untrusted tool results and non-assistant events are never evidence."""
    import json

    line = acceptance.PREFIX + json.dumps(complete_report())
    events = [
        {"type": "tool_use", "part": {"type": "tool", "text": line},
         "metadata": {"output": line}},
        {"type": "step_finish", "part": {"type": "step-finish"},
         "metadata": {"output": line}},
        {"type": "text", "part": {"type": "tool", "text": line}},
        {"type": "text", "part": {"type": "text", "text": line}},
    ]
    log = "\n".join(json.dumps(event) for event in events)
    assert acceptance.report_from_text(log) == complete_report()
    assert acceptance.report_from_text(
        "\n".join(json.dumps(event) for event in events[:-1])
    ) is None


def test_multiple_jsonl_assistant_reports_fail_closed():
    """Two reports are ambiguous even if the two copies are identical."""
    import json

    line = acceptance.PREFIX + json.dumps(complete_report())
    event = json.dumps({"type": "text", "part": {"type": "text", "text": line}})
    assert acceptance.report_from_text(event + "\n" + event) is None
    assert acceptance.report_from_text(line + "\n" + event) is None


def test_changed_path_citations_allow_numeric_line_ranges_only():
    """Line spans are review evidence, not changes to the actual path."""
    report = complete_report()
    for span in (":21", ":21-45", ":21-45,62-68"):
        report["items"][0]["file"] = FILE + span
        assert errors(report) == []
    for invalid in (FILE + " tests", FILE + ":21-45 notes", "other.py:21-45", []):
        report["items"][0]["file"] = invalid
        assert "cite a file changed" in " ".join(errors(report))


def test_mission_sends_complete_non_approving_schema_for_every_criterion():
    mission = acceptance.mission(4010, HEAD, ISSUE)
    assert "ACCEPTANCE_CRITERIA_COUNT=3" in mission
    assert "literal FACTORY_ACCEPTANCE_V1=" in mission
    assert "A1 through A3" in mission
    assert mission.count('"status":"unverified"') == 3
    assert all(f'"id":"A{i}"' in mission for i in range(1, 4))
    assert "FACTORY_GATE_BLOCKED" in mission
    assert acceptance.report_matches_mission(complete_report(), mission)


def test_mission_shape_rejects_real_composite_failure_and_stale_evidence():
    import copy
    import json

    mission = acceptance.mission(4010, HEAD, ISSUE)
    full = complete_report()
    # The live OpenRouter review returned bare JSON with only A1 and READY.
    assert acceptance.report_from_text(json.dumps(full) + "\nFACTORY_GATE_READY") is None
    partial = copy.deepcopy(full)
    partial["items"] = partial["items"][:1]
    assert not acceptance.report_matches_mission(partial, mission)
    assert not acceptance.report_matches_mission(
        acceptance.report_from_text(acceptance.PREFIX + json.dumps(partial)), mission
    )
    for field, value in (
        ("issue", 4011),
        ("head", NEXT),
        ("issue_digest", "c" * 64),
    ):
        stale = copy.deepcopy(full)
        stale[field] = value
        assert not acceptance.report_matches_mission(stale, mission)
    unverified = copy.deepcopy(full)
    unverified["items"][1]["status"] = "unverified"
    assert not acceptance.report_matches_mission(unverified, mission)
    reordered = copy.deepcopy(full)
    reordered["items"][0], reordered["items"][1] = (
        reordered["items"][1], reordered["items"][0]
    )
    assert not acceptance.report_matches_mission(reordered, mission)
    assert not acceptance.report_matches_mission(full, "")
