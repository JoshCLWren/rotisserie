"""Acceptance parsing: context headings, split citations and verify-after-merge criteria."""

import importlib
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
acceptance = importlib.import_module("factory_issue_acceptance")
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "acceptance_issue_5692.json").read_text()
)
HEAD = "a" * 40
CHANGED = ("frontend/src/a.tsx", "frontend/src/b.tsx")
DEPLOY_ISSUE = (
    "## Acceptance criteria\n"
    "- [ ] The cover renders for a blocked series.\n"
    "- [ ] Verify the fix in an authenticated deployed browser and record\n"
)


def item(index, *, status="pass", file=CHANGED[0]):
    return {
        "id": f"A{index}",
        "status": status,
        "file": file,
        "evidence": "The changed component remounts the image when its source changes.",
        "test": "vitest run src/unit/a.test.tsx covers the blocked series row",
    }


def report_for(body, items, head=HEAD, issue=4010):
    return {"issue": issue, "head": head, "issue_digest": acceptance.digest(body), "items": items}


def check(body, items, *, non_closing=False, changed=CHANGED):
    return acceptance.validate(
        report_for(body, items), 4010, HEAD, body, changed, non_closing=non_closing
    )


def test_issue_5692_context_sections_are_not_criteria():
    criteria = acceptance.requirements(FIXTURE["issue_body"])
    assert len(criteria) == 23 < len(FIXTURE["report"]["items"])
    for text in criteria:
        assert not text.startswith("Existing implementation / related work")
        assert not text.startswith("Original reporter evidence")
    assert criteria[-1].startswith("Testable acceptance criteria: Verify the actual fix")


def test_heading_suffixes_still_mark_context_but_criteria_words_win():
    body = (
        "## Scope\n- Render the cover.\n\n"
        "## Existing implementation / related work\n- PR #5476 added the projection.\n\n"
        "## Original reporter evidence (verbatim)\n- Cover for FF\n\n"
        "## Original report / diagnostics\n- Console errors: none\n\n"
        "## Reproduction steps\n- Sign in.\n\n"
        "## Bug: Background sync drops covers\n- Sync keeps every cover.\n\n"
        "## Acceptance criteria / notes\n- The job retries twice.\n"
    )
    assert acceptance.requirements(body) == [
        "Scope: Render the cover.",
        "Bug: Background sync drops covers: Sync keeps every cover.",
        "Acceptance criteria / notes: The job retries twice.",
    ]


def test_issue_5692_replay_splits_citations_and_defers_production_check():
    body, report = FIXTURE["issue_body"], FIXTURE["report"]
    replay = {**report, "items": report["items"][:23]}
    errors = acceptance.validate(
        replay, 5692, report["head"], body, FIXTURE["changed_paths"], non_closing=False
    )
    failed = [error.split(":", 1)[0] for error in errors]
    assert failed == ["A2", "A3", "A5", "A6", "A7", "A9", "A11", "A12", "A15", "A17", "A21"]
    assert all("cite a file changed by this PR (not changed:" in error for error in errors)
    assert [name for name, _ in acceptance.deferred(body)] == ["A23"]


def test_comma_semicolon_and_and_citations_each_must_be_changed():
    body = "## Scope\n- Render the cover.\n"
    for joined in (
        "frontend/src/a.tsx, frontend/src/b.tsx",
        "frontend/src/a.tsx; frontend/src/b.tsx",
        "frontend/src/a.tsx and frontend/src/b.tsx:10-20",
    ):
        assert check(body, [item(1, file=joined)]) == [], joined
    errors = check(body, [item(1, file="frontend/src/a.tsx, native/src/threads.rs")])
    assert errors == ["A1: cite a file changed by this PR (not changed: native/src/threads.rs)"]
    assert check(body, [item(1, file="frontend/src/a.tsx:191-342,245-336")]) == []


def test_deploy_only_criterion_does_not_block_approval_but_stays_recorded():
    items = [item(1), item(2, status="unverified", file="")]
    assert check(DEPLOY_ISSUE, items, non_closing=False) == []
    deferred = acceptance.deferred(DEPLOY_ISSUE)
    assert [name for name, _ in deferred] == ["A2"]
    assert "A2: Acceptance criteria: Verify the fix" in acceptance.deferred_note(DEPLOY_ISSUE)
    mission = acceptance.mission(4010, HEAD, DEPLOY_ISSUE)
    assert "\nA2 [verify_after_merge]: Acceptance criteria: Verify the fix" in mission
    assert "\nA1: Acceptance criteria: The cover renders" in mission
    shaped = report_for(DEPLOY_ISSUE, [item(1), item(2, status="deferred")])
    assert acceptance.report_matches_mission(shaped, mission)


def test_real_unmet_criterion_still_blocks():
    for status in ("deferred", "unverified", "remaining"):
        errors = check(DEPLOY_ISSUE, [item(1, status=status), item(2, status="deferred")])
        assert errors == ["A1: missing PASS evidence"], status
    body = (
        "## Scope\n- Fix the production crash and verify it with a regression test.\n"
        "- Verify production code handles a null cover URL.\n"
        "- Verified on production: the queue shows no cover.\n"
        "- Post-deploy QA of PR #4521 found the regression.\n"
    )
    assert acceptance.deferred(body) == []
    errors = check(body, [item(1, file="native/src/threads.rs"), item(2), item(3), item(4)])
    assert errors == ["A1: cite a file changed by this PR (not changed: native/src/threads.rs)"]


def test_rejection_summary_counts_every_failure():
    failures = [f"A{index}: cite a file changed by this PR" for index in range(1, 26)]
    summary = acceptance.failure_summary(failures)
    assert summary.startswith("25 problem(s): A1: cite")
    assert "A20: cite" in summary and "A21: cite" not in summary
    assert summary.endswith("; and 5 more")
    assert acceptance.failure_summary(failures[:3]) == "3 problem(s): " + "; ".join(failures[:3])
