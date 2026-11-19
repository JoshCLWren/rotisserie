"""Regression tests for factory issue implementation integrity (#4042)."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pytest import MonkeyPatch

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

delivery = importlib.import_module("factory_implementation_delivery")
HEAD = "a" * 40
SOURCE = "app/services/reading_list_cycle_validator.py"
FRONT = "frontend/src/pages/CblBrowserPage.tsx"


def patch(path: str, old: list[str], new: list[str]) -> str:
    """Produce a per-file diff containing supplied lines and context.

    Args:
        path: Changed source path.
        old: Complete old source lines.
        new: Complete new source lines.

    Returns:
        Unified patch with the full file context.
    """
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(old)} +1,{len(new)} @@\n"
        + "".join("-" + line + "\n" for line in old)
        + "".join("+" + line + "\n" for line in new)
    )


def test_docs_only_implementation_remains_open() -> None:
    """The #4040 docs-only PR cannot deliver CBL browser behavior."""
    docs = "docs/issue-plans/4010.md"
    ok, reason = delivery.pr_delivers_implementation(
        "Implement folder UI, navigation and source preview",
        [docs], patch(docs, ["Plan"], ["Revised plan"]),
    )
    assert not ok
    assert "production source" in reason


def test_docstring_only_implementation_remains_open() -> None:
    """The #4034 source docstring change must not qualify."""
    old = ['def validate():', '    """Old explanation', '    descriptive text."""',
           "    return False"]
    new = ['def validate():', '    """New explanation', '    different descriptive text."""',
           "    return False"]
    assert not delivery.pr_delivers_implementation(
        "Implement overlap-aware cycle validation", [SOURCE], patch(SOURCE, old, new)
    )[0]


def test_javascript_block_comment_is_not_code() -> None:
    """JS-style multiline comments are excluded."""
    old = ["/* Old", "   comment */", "export const ready = false;"]
    new = ["/* New", "   documentation only */", "export const ready = false;"]
    assert not delivery.pr_delivers_implementation(
        "Add new client behavior", [FRONT], patch(FRONT, old, new)
    )[0]


def test_rust_source_counts_as_production_implementation() -> None:
    """Rust-only Roll changes must not be treated as docs/test-only delivery."""
    path = "native/roll-service/src/roll_pool.rs"
    old = ["// old comment", "pub fn pool() -> i32 {", "    1", "}"]
    new = ["// new comment", "pub fn pool() -> i32 {", "    2", "}"]
    ok, reason = delivery.pr_delivers_implementation(
        "Fix the Roll in-play pool", [path], patch(path, old, new)
    )
    assert ok, reason

    comment_only = delivery.pr_delivers_implementation(
        "Fix the Roll in-play pool",
        [path],
        patch(path, ["// old comment", "pub fn pool() -> i32 { 1 }"],
              ["// new comment", "pub fn pool() -> i32 { 1 }"]),
    )
    assert not comment_only[0]


def test_docs_cannot_supply_evidence_for_comment_only_python() -> None:
    """A mixed docs/source diff must inspect only behavioral source."""
    docs = "docs/issue-plans/4017.md"
    diff = (
        patch(docs, ["Old prose"], ["New architecture guide"])
        + patch(SOURCE, ["# Old comment"], ["# New comment"])
    )
    assert not delivery.pr_delivers_implementation(
        "Implement new validator", [SOURCE, docs], diff
    )[0]


def test_deletion_only_fix_counts_as_behavior() -> None:
    """Removing a buggy early return is an implementation."""
    body = "Implement correct behavior by deleting early return"
    old = ["def validate():", "    return False", "    do_actual_work()"]
    new = ["def validate():", "    do_actual_work()"]
    assert delivery.pr_delivers_implementation(
        body, [SOURCE], patch(SOURCE, old, new)
    )[0]


def test_whitespace_only_change_not_delivery() -> None:
    """Reindentation without changed executable tokens is insufficient."""
    assert not delivery.pr_delivers_implementation(
        "Implement validation", [SOURCE],
        patch(SOURCE, ["    return False"], ["  return   False"]),
    )[0]


@pytest.mark.parametrize(
    "issue",
    [
        "Do not accept docs-only changes; implement the browser.",
        "Implement browser per PRD #123.",
        "Acceptance issue #900 was for planning. Implement this now.",
        "## Scope\n- Deliver executable behavior\n- No docs-only result.",
    ],
)
def test_incidental_doc_words_cannot_disable_gate(issue: str) -> None:
    """Mentioning documentation terms does not reclassify an issue."""
    assert delivery.is_implementation_issue(issue)


@pytest.mark.parametrize(
    "kind",
    ["docs-only", "documentation", "investigation", "prd", "acceptance-only"],
)
def test_explicit_docs_type_passes(kind: str) -> None:
    """Only a structured issue-type declaration allows non-code delivery."""
    assert not delivery.is_implementation_issue(
        f"**Issue type:** {kind}\n\nUpdate the requested document"
    )


def test_both_code_and_browser_test_pass_mechanical_gate() -> None:
    """Runnable implementation changes are not automatically rejected."""
    assert delivery.pr_delivers_implementation(
        "Implement CBL folder UI",
        [FRONT, "frontend/src/unit/CblBrowserPage.test.tsx"],
        patch(FRONT, ["return null;"], ["return <CBLBrowser />;"]),
    )[0]


def test_missing_diff_fails_closed() -> None:
    """Paths cannot be used as a proxy for executable code."""
    assert not delivery.pr_delivers_implementation(
        "Implement the search endpoint", [SOURCE]
    )[0]


def test_test_only_change_does_not_count_as_product_behavior() -> None:
    """A changed test does not by itself implement new product logic."""
    path = "tests/test_new_search.py"
    assert not delivery.pr_delivers_implementation(
        "Implement the search endpoint", [path],
        patch(path, ["assert False"], ["assert True"]),
    )[0]


def load_controller() -> ModuleType:
    """Load the hyphenated production review controller.

    Returns:
        Imported production controller module.
    """
    path = SCRIPTS / "factory-review-controller.py"
    spec = importlib.util.spec_from_file_location("factory_review_controller", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_authoritative_review_gate_rejects_docs_only_implementation(
    monkeypatch: MonkeyPatch,
) -> None:
    """The real controller gate, not just its helper, refuses docs-only PRs."""
    module = load_controller()
    doc = "docs/issue-plans/4010.md"
    current = {
        "state": "OPEN",
        "headRefName": "factory/44-4010-opencode-free",
        "headRefOid": HEAD,
    }
    monkeypatch.setattr(module, "pr_json", lambda _pr: current)
    monkeypatch.setattr(module, "target_json", lambda _n: {
        "state": "open", "body": "## Scope\n- Implement working CBL browser"
    })
    monkeypatch.setattr(module, "model_comment_bodies", lambda _pr: [])

    monkeypatch.setattr(module.implementation_delivery, "complete_pr_diff", lambda *_args: (
        [doc], patch(doc, ["Plan"], ["Edited plan"])
    ))
    verdict = module.issue_acceptance_gate(4040, HEAD)
    assert verdict["decision"] == "deny"
    assert "production source" in verdict["reason"]


def test_current_head_validation_rejects_racing_edits(
    monkeypatch: MonkeyPatch,
) -> None:
    """No stale-head executable evidence can be carried into approval."""
    module = load_controller()
    monkeypatch.setattr(module, "pr_json", lambda _pr: {"headRefOid": "b" * 40})
    monkeypatch.setattr(module.implementation_delivery, "complete_pr_diff", lambda *_args: (
        [SOURCE], patch(SOURCE, ["return False"], ["return True"])
    ))
    verdict = module.implementation_delivery_gate(44, HEAD, "Implement validation")
    assert verdict["decision"] == "retry"
    assert "changed" in verdict["reason"]


@pytest.mark.parametrize("path", ["Dockerfile", "Makefile", ".github/workflows/ci.yml"])
def test_behavioral_build_files_are_implementation(path: str) -> None:
    """Build and CI behavior must not require an unrelated application edit."""
    assert delivery.pr_delivers_implementation(
        "Repair the build", [path], patch(path, ["run: false"], ["run: true"])
    )[0]


@pytest.mark.parametrize("old,new", [
    ('QUERY = """SELECT 1"""', 'QUERY = """SELECT 2"""'),
    ('MESSAGE = "two words"', 'MESSAGE = "twowords"'),
    ('QUERY = """SELECT\n    1\n"""', 'QUERY = """SELECT\n    2\n"""'),
    ('URL = "https://a/#one"', 'URL = "https://a/#two"'),
])
def test_python_string_values_are_behavioral(old: str, new: str) -> None:
    """Literal contents, including spaces and comment markers, affect behavior."""
    assert delivery.pr_delivers_implementation(
        "Fix returned text", [SOURCE], patch(SOURCE, old.splitlines(), new.splitlines())
    )[0]


def test_long_docstring_and_block_comment_changes_are_not_behavior() -> None:
    """Complete-file context handles edits far from opening delimiters."""
    for path, opening, closing in ((SOURCE, '"""', '"""'), (FRONT, '/*', '*/')):
        old = [opening] + [f"explanation {i}" for i in range(30)] + [closing]
        new = old.copy()
        new[15] = "changed prose"
        assert not delivery.pr_delivers_implementation(
            "Implement browsing", [path], patch(path, old, new)
        )[0]


def test_complete_diff_uses_immutable_sources_and_preserves_no_newline() -> None:
    """The reader obtains whole files at merge base/head, including final lines."""
    import json

    calls: list[list[str]] = []

    def read(args: list[str]) -> str:
        calls.append(args)
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({"head": {"sha": HEAD}, "base": {"sha": "base"}})
        if "/compare/" in endpoint:
            return json.dumps({"merge_base_commit": {"sha": "ancestor"}})
        if endpoint.endswith("/files"):
            return json.dumps([[{"filename": SOURCE, "status": "modified",
                                 "patch": "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2"}]])
        return "VALUE = 1" if endpoint.endswith("ref=ancestor") else "VALUE = 2"

    paths, diff = delivery.complete_pr_diff(44, HEAD, "owner/repo", read)
    assert delivery.pr_delivers_implementation("Fix value", paths, diff)[0]
    assert any(f"ref={HEAD}" in command[-1] for command in calls)


def test_omitted_large_patch_reads_the_whole_base_file_instead_of_failing() -> None:
    """GitHub omits the patch for oversized changes; the base file still proves them."""
    import json

    artifact = "frontend/src/generated/openapi.json"
    old = '{"schema": "old"}\n'
    new = '{"schema": "new"}\n'

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": HEAD}, "base": {"sha": "base"},
            })
        if "/compare/" in endpoint:
            return json.dumps({"merge_base_commit": {"sha": "ancestor"}})
        if endpoint.endswith("/files"):
            # No `patch` field at all: this is how GitHub reports a change too
            # large to inline.
            return json.dumps([[{
                "filename": artifact, "status": "modified", "changes": 2,
            }]])
        if f"ref={HEAD}" in endpoint:
            return new
        if "ref=ancestor" in endpoint:
            return old
        raise AssertionError(f"unexpected read {endpoint}")

    paths, diff = delivery.complete_pr_diff(44, HEAD, "owner/repo", read)

    assert paths == [artifact]
    assert "-{\"schema\": \"old\"}" in diff
    assert "+{\"schema\": \"new\"}" in diff
    assert delivery.pr_delivers_implementation("Ship the contract", paths, diff)[0]


def test_omitted_patch_for_an_added_file_reads_no_base_content() -> None:
    """An added file has no base version; its diff starts from an empty file."""
    import json

    added = "app/services/new_reader.py"

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": HEAD}, "base": {"sha": "base"},
            })
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": added, "status": "added", "changes": 1,
            }]])
        if f"ref={HEAD}" in endpoint:
            return "VALUE = 1\n"
        raise AssertionError(f"unexpected read {endpoint}")

    paths, diff = delivery.complete_pr_diff(44, HEAD, "owner/repo", read)

    assert paths == [added]
    assert "+VALUE = 1" in diff
    assert delivery.pr_delivers_implementation("Add the reader", paths, diff)[0]


def test_omitted_patch_for_a_removed_file_reads_the_base_content() -> None:
    """A removed file's history is its base content; the head contributes nothing."""
    import json

    removed = "app/services/retired_reader.py"

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": HEAD}, "base": {"sha": "base"},
            })
        if "/compare/" in endpoint:
            return json.dumps({"merge_base_commit": {"sha": "ancestor"}})
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": removed, "status": "removed", "changes": 1,
            }]])
        if "ref=ancestor" in endpoint:
            return "VALUE = 1\n"
        raise AssertionError(f"unexpected read {endpoint}")

    paths, diff = delivery.complete_pr_diff(44, HEAD, "owner/repo", read)

    assert paths == [removed]
    assert "-VALUE = 1" in diff


def test_unavailable_base_source_still_fails_closed() -> None:
    """A missing base file cannot be guessed into behavioral evidence."""
    import json

    artifact = "frontend/src/generated/openapi.json"

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": HEAD}, "base": {"sha": "base"},
            })
        if "/compare/" in endpoint:
            return json.dumps({"merge_base_commit": {"sha": "ancestor"}})
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": artifact, "status": "modified", "changes": 2,
            }]])
        if f"ref={HEAD}" in endpoint:
            return "new\n"
        raise RuntimeError("base content unavailable")

    with pytest.raises(RuntimeError, match="source patch unavailable"):
        delivery.complete_pr_diff(44, HEAD, "owner/repo", read)


def test_missing_base_commit_still_fails_closed() -> None:
    """Without a base commit there is no immutable pre-change source to read."""
    import json

    artifact = "frontend/src/generated/openapi.json"

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({"head": {"sha": HEAD}})
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": artifact, "status": "modified", "changes": 2,
            }]])
        return "new\n"

    with pytest.raises(RuntimeError, match="source patch unavailable"):
        delivery.complete_pr_diff(44, HEAD, "owner/repo", read)


def test_omitted_patch_fallback_matches_the_patch_reversal_evidence() -> None:
    """Both reader paths reconstruct the same pre-change source for one file."""
    import json

    artifact = "frontend/src/generated/openapi.json"
    old = "line one\nline two\n"
    new = "line one\nline changed\n"
    hunk = "@@ -1,2 +1,2 @@\n line one\n-line two\n+line changed\n"

    def patched_read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": HEAD}, "base": {"sha": "base"},
            })
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": artifact, "status": "modified", "changes": 2,
                "patch": hunk,
            }]])
        if f"ref={HEAD}" in endpoint:
            return new
        raise AssertionError(f"unexpected read {endpoint}")

    def omitted_read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": HEAD}, "base": {"sha": "base"},
            })
        if "/compare/" in endpoint:
            return json.dumps({"merge_base_commit": {"sha": "ancestor"}})
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": artifact, "status": "modified", "changes": 2,
            }]])
        if f"ref={HEAD}" in endpoint:
            return new
        if "ref=ancestor" in endpoint:
            return old
        raise AssertionError(f"unexpected read {endpoint}")

    _, patched_diff = delivery.complete_pr_diff(44, HEAD, "owner/repo", patched_read)
    _, omitted_diff = delivery.complete_pr_diff(44, HEAD, "owner/repo", omitted_read)

    assert patched_diff == omitted_diff



def test_omitted_patch_uses_merge_base_not_advanced_main_tip() -> None:
    """Base branch updates must never look like authored PR changes."""
    import json

    artifact = "frontend/src/generated/openapi.json"
    calls: list[str] = []

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        calls.append(endpoint)
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": HEAD}, "base": {"sha": "advanced-main"},
            })
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": artifact, "status": "modified", "changes": 2,
            }]])
        if "/compare/advanced-main..." in endpoint:
            return json.dumps({"merge_base_commit": {"sha": "ancestor"}})
        if f"ref={HEAD}" in endpoint:
            return "VALUE = 2\n"
        if "ref=ancestor" in endpoint:
            return "VALUE = 1\n"
        if "ref=advanced-main" in endpoint:
            raise AssertionError("moving main tip used instead of merge base")
        raise AssertionError(f"unexpected read {endpoint}")

    _, diff = delivery.complete_pr_diff(44, HEAD, "owner/repo", read)

    assert "-VALUE = 1" in diff
    assert "+VALUE = 2" in diff
    assert any("/compare/advanced-main..." in call for call in calls)
    assert not any("ref=advanced-main" in call for call in calls)


def test_omitted_patch_without_verified_merge_base_fails_closed() -> None:
    """A missing merge-base identity cannot authorize source evidence."""
    import json

    artifact = "frontend/src/generated/openapi.json"

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": HEAD}, "base": {"sha": "advanced-main"},
            })
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": artifact, "status": "modified", "changes": 2,
            }]])
        if "/compare/" in endpoint:
            return json.dumps({"merge_base_commit": {}})
        if f"ref={HEAD}" in endpoint:
            return "new\n"
        raise AssertionError(f"unexpected base-file read {endpoint}")

    with pytest.raises(RuntimeError, match="source patch unavailable"):
        delivery.complete_pr_diff(44, HEAD, "owner/repo", read)


def test_omitted_patch_on_merged_head_fails_closed() -> None:
    """An already-merged head cannot stand in for its original pre-change source."""
    import json

    artifact = "frontend/src/generated/openapi.json"

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": HEAD}, "base": {"sha": "main-now"},
            })
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": artifact, "status": "modified", "changes": 2,
            }]])
        if "/compare/" in endpoint:
            return json.dumps({"merge_base_commit": {"sha": HEAD}})
        if f"ref={HEAD}" in endpoint:
            return "new\n"
        raise AssertionError(f"unexpected base-file read {endpoint}")

    with pytest.raises(RuntimeError, match="source patch unavailable"):
        delivery.complete_pr_diff(44, HEAD, "owner/repo", read)


def test_head_change_still_fails_closed_before_any_source_read() -> None:
    """A racing head is rejected before any base content is requested."""
    import json

    reads: list[list[str]] = []

    def read(args: list[str]) -> str:
        reads.append(args)
        if args[-1].endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": "b" * 40}, "base": {"sha": "base"},
            })
        raise AssertionError(f"unexpected read {args[-1]}")

    with pytest.raises(RuntimeError, match="head changed"):
        delivery.complete_pr_diff(44, HEAD, "owner/repo", read)
    assert all("/files" not in command[-1] for command in reads)


@pytest.mark.parametrize("closed", [False, True])
def test_post_merge_refuses_non_delivery_and_repairs_keyword_closure(
    monkeypatch: MonkeyPatch, closed: bool,
) -> None:
    """Both open issues and prematurely auto-closed issues retain implementation work."""
    closure = importlib.import_module("factory_post_merge_closure")

    monkeypatch.setattr(closure, "_pr_info", lambda _pr: {
        "branch": "factory/46-4042-kilo-auto", "body": "Closes #4042",
        "state": "MERGED", "head": HEAD,
    })
    monkeypatch.setattr(closure, "_issue_view", lambda _issue: {
        "state": "CLOSED" if closed else "OPEN", "body": "Implement browsing",
        "labels": ["factory", "ralph-status:done", "factory:unowned"],
    })
    monkeypatch.setattr(closure, "_issue_has_open_successor_pr", lambda *_args: False)
    monkeypatch.setattr(closure, "_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(closure, "_closed_by_pr", lambda *_args: True)
    monkeypatch.setattr(delivery, "complete_pr_diff", lambda *_args: (
        ["docs/plan.md"], patch("docs/plan.md", ["old"], ["new"])
    ))
    mutations: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(closure, "_run_gh", lambda *args: mutations.append(("gh", args)))
    monkeypatch.setattr(
        closure, "_replace_labels", lambda *args: mutations.append(("labels", args))
    )
    monkeypatch.setattr(
        closure, "_comment_issue", lambda *args: mutations.append(("comment", args))
    )
    monkeypatch.setattr(closure, "_close_issue", lambda *_args: pytest.fail("must not close"))
    assert closure.close_merged_pr_issue(4046).startswith("not-delivered:")
    # A closed issue additionally has its dependency-block marker rechecked via
    # a comments read, which inspects the issue without mutating it.
    label_mutations = [args for name, args in mutations if name == "labels"]
    assert bool(label_mutations) == closed
    if closed:
        assert any(
            any("reopen" in str(part) for part in args)
            for name, args in mutations if name == "gh"
        )
        labels = label_mutations[0][1]
        assert isinstance(labels, list)
        assert "ralph-status:pending" in labels


@pytest.mark.parametrize("current,change,previous", [
    ("first\nlast", "@@ -2 +1,0 @@\n-removed", "first\nremoved\nlast"),
    ("", "@@ -1,2 +0,0 @@\n-first\n-second", "first\nsecond"),
    ("added", "@@ -0,0 +1 @@\n+added", ""),
])
def test_reverse_patch_handles_zero_length_sides(
    current: str, change: str, previous: str,
) -> None:
    """Added/deleted files and deletion-only hunks use Git's zero-count offsets."""
    assert delivery._previous_source(current, change) == previous


def test_truncated_source_patch_fails_closed() -> None:
    """GitHub's missing/truncated patch cannot become behavioral evidence."""
    with pytest.raises(RuntimeError, match="incomplete"):
        delivery._previous_source("new", "@@ -1,2 +1 @@\n-old\n+new")


@pytest.mark.parametrize(
    "path,old,new",
    [
        ("Dockerfile", "RUN false", "RUN true"),
        ("Makefile", "build: old", "build: new"),
        (".github/workflows/ci.yml", "run: old", "run: new"),
        ("pyproject.toml", "requires-python = '>=3.11'",
         "requires-python = '>=3.14'"),
    ],
)
def test_build_and_workflow_fixes_are_code(
    path: str, old: str, new: str
) -> None:
    """Build and workflow edits can be valid executable deliverables."""
    assert delivery.pr_delivers_implementation(
        "Fix build and workflow behavior", [path],
        patch(path, [old], [new]),
    )[0]


@pytest.mark.parametrize(
    "old,new",
    [
        ('QUERY = """SELECT 1"""', 'QUERY = """SELECT 2"""'),
        ("QUERY = 'SELECT first column'", "QUERY = 'SELECT second column'"),
        ("message = 'hello world'", "message = 'hello  world'"),
    ],
)
def test_behavioral_string_value_edits_are_preserved(
    old: str, new: str
) -> None:
    """Meaningful string edits survive code token comparison."""
    assert delivery.pr_delivers_implementation(
        "Fix SQL query or message", [SOURCE],
        patch(SOURCE, [old], [new]),
    )[0]


def test_marker_helpers_treat_none_payload_as_no_marker(
    monkeypatch: MonkeyPatch,
) -> None:
    """A None gh payload (as mocks and interrupted calls produce) is not a crash."""
    closure = importlib.import_module("factory_post_merge_closure")
    monkeypatch.setattr(closure, "_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(closure, "_run_gh", lambda *args: None)
    assert closure._has_closure_marker(4042, 4046) is False
    assert closure._has_dependency_block_marker(4042, 4046) is False
    assert closure._has_unreadable_dependency_warning(4042, 4046) is False


def test_reopened_blocked_issue_keeps_live_owner_when_deps_finish(
    monkeypatch: MonkeyPatch,
) -> None:
    """A claimed reopened issue is neither relabeled nor closed by the sweep (G2)."""
    closure = importlib.import_module("factory_post_merge_closure")

    monkeypatch.setattr(closure, "_pr_info", lambda _pr: {
        "branch": "factory/41-4017-kilo-auto", "body": "Closes #4017",
        "state": "MERGED", "head": HEAD, "merge": "merge",
    })
    monkeypatch.setattr(closure, "_issue_view", lambda _issue: {
        "state": "OPEN", "body": "Delivered work.\nDepends on #4016",
        "labels": ["enhancement", "factory", "factory:blocked",
                   "ralph-status:blocked", "factory:7"],
    })
    monkeypatch.setattr(closure, "_closed_by_pr", lambda *_args: False)
    monkeypatch.setattr(closure, "_has_dependency_block_marker", lambda *_args: True)
    monkeypatch.setattr(closure, "_native_open_blockers", lambda *_args: [])
    monkeypatch.setattr(
        closure.eligibility, "parse_declared_dependencies", lambda _body: {4016}
    )
    monkeypatch.setattr(delivery, "complete_pr_diff", lambda *_args: (
        ["app/fix.py"], patch("app/fix.py", ["old = 1"], ["new = 2"])
    ))
    mutations: list[tuple[object, ...]] = []
    monkeypatch.setattr(closure, "_replace_labels", lambda *args: mutations.append(args))
    monkeypatch.setattr(closure, "_close_issue", lambda *_args: mutations.append("close"))
    assert closure.close_merged_pr_issue(4034) == "active-work"
    assert mutations == []


def test_reopened_blocked_issue_keeps_open_successor_when_deps_finish(
    monkeypatch: MonkeyPatch,
) -> None:
    """An open successor PR protects the reopened issue from dependency closure (G2)."""
    closure = importlib.import_module("factory_post_merge_closure")

    monkeypatch.setattr(closure, "_pr_info", lambda _pr: {
        "branch": "factory/41-4017-kilo-auto", "body": "Closes #4017",
        "state": "MERGED", "head": HEAD, "merge": "merge",
    })
    monkeypatch.setattr(closure, "_issue_view", lambda _issue: {
        "state": "OPEN", "body": "Delivered work.\nDepends on #4016",
        "labels": ["enhancement", "factory", "factory:blocked",
                   "ralph-status:blocked", "factory:unowned"],
    })
    monkeypatch.setattr(closure, "_closed_by_pr", lambda *_args: False)
    monkeypatch.setattr(closure, "_has_dependency_block_marker", lambda *_args: True)
    monkeypatch.setattr(closure, "_issue_has_open_successor_pr", lambda *_args: True)
    monkeypatch.setattr(closure, "_native_open_blockers", lambda *_args: [])
    monkeypatch.setattr(
        closure.eligibility, "parse_declared_dependencies", lambda _body: {4016}
    )
    monkeypatch.setattr(delivery, "complete_pr_diff", lambda *_args: (
        ["app/fix.py"], patch("app/fix.py", ["old = 1"], ["new = 2"])
    ))
    mutations: list[tuple[object, ...]] = []
    monkeypatch.setattr(closure, "_replace_labels", lambda *args: mutations.append(args))
    monkeypatch.setattr(closure, "_close_issue", lambda *_args: mutations.append("close"))
    assert closure.close_merged_pr_issue(4034) == "successor-open"
    assert mutations == []


def test_reclosed_issue_restores_labels_despite_old_closure_marker(
    monkeypatch: MonkeyPatch,
) -> None:
    """A re-completed issue never keeps blocked labels because a marker exists (G3)."""
    closure = importlib.import_module("factory_post_merge_closure")

    comments: list[str] = []
    monkeypatch.setattr(closure, "_pr_info", lambda _pr: {
        "branch": "factory/41-4017-kilo-auto", "body": "Closes #4017",
        "state": "MERGED", "head": HEAD, "merge": "merge",
    })
    monkeypatch.setattr(closure, "_issue_view", lambda _issue: {
        "state": "CLOSED", "body": "Delivered work.\nDepends on #4016",
        "labels": ["enhancement", "factory", "factory:blocked",
                   "ralph-status:blocked", "factory:unowned"],
    })
    monkeypatch.setattr(closure, "_closed_by_pr", lambda *_args: True)
    monkeypatch.setattr(closure, "_has_dependency_block_marker", lambda *_args: True)
    monkeypatch.setattr(closure, "_has_closure_marker", lambda *_args: True)
    monkeypatch.setattr(closure, "_issue_has_open_successor_pr", lambda *_args: False)
    monkeypatch.setattr(closure, "_native_open_blockers", lambda *_args: [])
    monkeypatch.setattr(
        closure.eligibility, "parse_declared_dependencies", lambda _body: {4016}
    )
    monkeypatch.setattr(delivery, "complete_pr_diff", lambda *_args: (
        ["app/fix.py"], patch("app/fix.py", ["old = 1"], ["new = 2"])
    ))
    replaced: list[list[str]] = []
    monkeypatch.setattr(closure, "_replace_labels", lambda _i, labels: replaced.append(labels))
    monkeypatch.setattr(
        closure, "_comment_issue", lambda _i, body: comments.append(body)
    )
    closed_as: list[object] = []
    monkeypatch.setattr(closure, "_close_issue", lambda *_args: closed_as.append(True))
    assert closure.close_merged_pr_issue(4034) == "closed"
    assert comments == []
    assert closed_as == []
    assert replaced
    assert "factory:blocked" not in replaced[0]
    assert "ralph-status:blocked" not in replaced[0]
    assert "ralph-status:done" in replaced[0]


def test_unreadable_dependency_warning_is_deduplicated(
    monkeypatch: MonkeyPatch,
) -> None:
    """A posted unreadable-dependency warning never reposts on later sweeps (G4)."""
    closure = importlib.import_module("factory_post_merge_closure")

    monkeypatch.setattr(closure, "_pr_info", lambda _pr: {
        "branch": "factory/33-4025-kilo-auto", "body": "Closes #4025",
        "state": "MERGED", "head": HEAD, "merge": "merge",
    })
    monkeypatch.setattr(closure, "_issue_view", lambda _issue: {
        "state": "CLOSED", "body": "Delivered work.\nDepends on #4024",
        "labels": ["enhancement", "factory", "factory:blocked",
                   "ralph-status:blocked", "factory:unowned"],
    })
    monkeypatch.setattr(closure, "_closed_by_pr", lambda *_args: True)
    monkeypatch.setattr(closure, "_has_dependency_block_marker", lambda *_args: True)
    monkeypatch.setattr(
        closure, "_has_unreadable_dependency_warning", lambda *_args: True
    )
    monkeypatch.setattr(closure, "_run_gh", lambda *args: "[]")

    def unreadable_blockers(*_args: object) -> list[int]:
        raise RuntimeError("gh failed")

    monkeypatch.setattr(closure, "_native_open_blockers", unreadable_blockers)
    monkeypatch.setattr(
        closure.eligibility, "parse_declared_dependencies", lambda _body: {4024}
    )
    monkeypatch.setattr(delivery, "complete_pr_diff", lambda *_args: (
        ["app/fix.py"], patch("app/fix.py", ["old = 1"], ["new = 2"])
    ))
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(closure, "_replace_labels", lambda *args: events.append(args))
    monkeypatch.setattr(
        closure, "_comment_issue", lambda *_args: events.append("comment")
    )
    monkeypatch.setattr(closure, "_close_issue", lambda *_args: events.append("close"))
    assert closure.close_merged_pr_issue(4041) == "open-dependency"
    assert events
    labels = events[0][1]
    assert isinstance(labels, list)
    assert "ralph-status:pending" in labels
    assert "factory:unowned" in labels
    assert events[1:] == []


def test_keyword_closed_multiple_deps_one_open_reopens_blocked(
    monkeypatch: MonkeyPatch,
) -> None:
    """A multi-dependency issue that stays open with one open prerequisite reopens blocked (A9)."""
    closure = importlib.import_module("factory_post_merge_closure")

    comments: list[str] = []

    def issue_view(number: int) -> dict[str, object]:
        state = "OPEN" if number == 4026 else "CLOSED"
        return {
            "state": state,
            "body": "Delivered work.\nDepends on #4023\nDepends on #4026",
            "labels": ["enhancement", "factory", "factory:unowned"],
        }

    monkeypatch.setattr(closure, "_pr_info", lambda _pr: {
        "branch": "factory/8-4027-kilo-auto", "body": "Closes #4027",
        "state": "MERGED", "head": HEAD, "merge": "merge",
    })
    monkeypatch.setattr(closure, "_issue_view", issue_view)
    monkeypatch.setattr(closure, "_closed_by_pr", lambda *_args: True)
    monkeypatch.setattr(closure, "_has_dependency_block_marker", lambda *_args: False)
    monkeypatch.setattr(closure, "_has_closure_marker", lambda *_args: False)
    monkeypatch.setattr(closure, "_issue_has_open_successor_pr", lambda *_args: False)
    monkeypatch.setattr(closure, "_run_gh", lambda *args: "[]")
    monkeypatch.setattr(closure, "_native_open_blockers", lambda *_args: [])
    monkeypatch.setattr(
        closure.eligibility, "parse_declared_dependencies", lambda _body: {4023, 4026}
    )
    monkeypatch.setattr(delivery, "complete_pr_diff", lambda *_args: (
        ["app/fix.py"], patch("app/fix.py", ["old = 1"], ["new = 2"])
    ))
    monkeypatch.setattr(
        closure, "_comment_issue", lambda _i, body: comments.append(body)
    )
    replaced: list[list[str]] = []
    monkeypatch.setattr(closure, "_replace_labels", lambda _i, labels: replaced.append(labels))
    closed_as: list[object] = []
    monkeypatch.setattr(closure, "_close_issue", lambda *_args: closed_as.append(True))
    assert closure.close_merged_pr_issue(4043) == "open-dependency: #4026"
    assert closed_as == []
    assert "factory:blocked" in replaced[0]
    assert "ralph-status:blocked" in replaced[0]
    assert "factory:unowned" in replaced[0]
    assert any("comic-pile-factory-dependency-block-v1:pr-4043:issue-4027:deps-4026"
               in body for body in comments)
    assert any("#4026" in body for body in comments)


@pytest.mark.parametrize("event,commit,expected", [
    ("closed", HEAD, True), ("closed", "other-delivery", False),
    ("closed", None, False), ("reopened", HEAD, False),
])
def test_only_own_keyword_closure_may_be_reopened(
    monkeypatch: MonkeyPatch, event: str, commit: str | None, expected: bool,
) -> None:
    """Historical non-delivery cannot undo a later delivery or manual closure."""
    import json

    closure = importlib.import_module("factory_post_merge_closure")
    monkeypatch.setattr(closure, "_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(closure, "_run_gh", lambda *_args: json.dumps([[
        {"event": "closed", "commit_id": HEAD}, {"event": event, "commit_id": commit},
    ]]))
    assert closure._closed_by_pr(4042, {"head": HEAD, "merge": "merge"}) is expected


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "Makefile"])
def test_indented_build_files_ignore_comment_lines_but_keep_structure(path: str) -> None:
    """Comment-only edits are excluded without losing significant indentation."""
    old = ["# old documentation", "build:", "  run: true"]
    new = ["build:", "  run: true"]
    assert not delivery.pr_delivers_implementation(
        "Fix build behavior", [path], patch(path, old, new)
    )[0]
    assert delivery.pr_delivers_implementation(
        "Fix build indentation", [path], patch(path, new, ["build:", "    run: true"])
    )[0]


@pytest.mark.parametrize("old,new", [
    ('if (( ${#prs[@]} > 1 )); then exit 1; fi',
     'if (( ${#prs[@]} > 2 )); then exit 1; fi'),
    ('echo ${value#prefix}', 'echo ${value#other}'),
    ('echo ${value:- #old}', 'echo ${value:- #new}'),
    ('echo ${value:-${other#old}}', 'echo ${value:-${other#new}}'),
    ('echo $#', 'echo $1'),
    ('echo foo#bar', 'echo foo#baz'),
    ('printf foo -bar', 'printf foo-bar'),
    ('echo first\necho second', 'echo first echo second'),
])
def test_shell_expansions_and_command_boundaries_are_behavioral(old: str, new: str) -> None:
    """Bash expansion hashes and word/command boundaries must survive comparison."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        "Fix shell behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("old,new", [
    ('# old comment\necho ok', '# new comment\necho ok'),
    ('echo ok # old', 'echo ok # new'),
    ('echo   ok', '  echo ok'),
    ('echo one; echo two', 'echo one\necho two'),
])
def test_shell_comments_and_formatting_do_not_qualify(old: str, new: str) -> None:
    """Comments and equivalent command formatting remain non-delivering."""
    path = '.github/scripts/worker.sh'
    assert not delivery.pr_delivers_implementation(
        "Fix shell behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("path,old,new", [
    ('frontend/src/styles.css', '.nav-item.active.nav-label { color: red; }',
     '.nav-item.active .nav-label { color: red; }'),
    ('frontend/src/styles.css', '.container* { color: red; }',
     '.container * { color: red; }'),
    ('frontend/src/client.ts', 'function f() { return value; }',
     'function f() { return\nvalue; }'),
    ('frontend/src/client.ts', 'let value = other;', 'let value = otherValue;'),
])
def test_language_significant_spacing_changes_qualify(path: str, old: str, new: str) -> None:
    """Selector descendants and JavaScript line terminators change behavior."""
    assert delivery.pr_delivers_implementation(
        "Fix behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("path,old,new", [
    ('frontend/src/styles.css', '.a { color: red; }', '.a{color:red;}'),
    ('frontend/src/styles.css', '.a { color : red; }', '.a{color:red;}'),
    ('frontend/src/styles.css', '.a { width: calc(2 * 3); }',
     '.a{width:calc(2*3);}'),
    ('frontend/src/client.ts', 'const value = 1;', 'const value=1;'),
    ('frontend/src/client.ts', 'function f() {\nreturn value;\n}',
     'function f(){return value;}'),
])
def test_ordinary_css_and_javascript_formatting_stays_non_delivery(
    path: str, old: str, new: str,
) -> None:
    """Spacing around punctuation and indentation alone does not deliver behavior."""
    assert not delivery.pr_delivers_implementation(
        "Fix behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


def test_shell_single_quoted_backslash_newline_is_literal() -> None:
    """Bash preserves both bytes inside a single-quoted argument."""
    path = '.github/scripts/worker.sh'
    old = "printf '%s' 'first\\\nsecond'"
    new = "printf '%s' 'firstsecond'"
    assert delivery.pr_delivers_implementation(
        "Fix shell output", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("old,new", [
    ('printf foo \\\nbar', 'printf foo bar'),
    ('printf "%s" "first\\\nsecond"', 'printf "%s" "firstsecond"'),
])
def test_real_shell_line_continuations_remain_formatting(old: str, new: str) -> None:
    """Unquoted and double-quoted Bash continuations do not alter output."""
    path = '.github/scripts/worker.sh'
    assert not delivery.pr_delivers_implementation(
        "Fix shell output", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("old,new", [
    ('let result = ++count;', 'let result =\n  ++count;'),
    ('let result = ++count;', 'let result = ++\n  count;'),
    ('let result = await ++count;', 'let result = await\n  ++count;'),
    ('const obj = { async: 1 };', 'const obj = { async\n: 1 };'),
    ('async(foo);', 'async\n(foo);'),
])
def test_prefix_increment_and_async_identifier_wrapping_is_formatting(old: str, new: str) -> None:
    """Line breaks before prefix operators and ordinary identifiers do not qualify."""
    path = 'frontend/src/client.ts'
    assert not delivery.pr_delivers_implementation(
        "Fix JS behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("old,new", [
    ('let result = count\n++other;', 'let result = count++\nother;'),
    ('async function f() {}', 'async\nfunction f() {}'),
    ('async x => x', 'async\nx => x'),
    ('async (x) => x', 'async\n(x) => x'),
])
def test_restricted_javascript_newlines_still_change_behavior(old: str, new: str) -> None:
    """Postfix and actual async productions retain line-terminator semantics."""
    path = 'frontend/src/client.ts'
    assert delivery.pr_delivers_implementation(
        "Fix JS behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("old,new", [
    ("printf '%s' $'it\\'s' foo", "printf '%s' $'it\\'s' \\\nfoo"),
    ("printf '%s' $'it\\'s' foo", "printf '%s' $'it\\'s' \\\n  foo"),
])
def test_ansi_c_escaped_quote_does_not_hide_real_continuation(old: str, new: str) -> None:
    """An escaped apostrophe in ANSI-C quotes does not reopen single quoting."""
    path = '.github/scripts/worker.sh'
    assert not delivery.pr_delivers_implementation(
        "Fix shell behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("old,new", [
    ("printf '%s' $'first\\\nsecond'", "printf '%s' $'firstsecond'"),
    ("printf '%s' $'it\\'s one'", "printf '%s' $'it\\'s  one'"),
    ("printf '%s' foo'one two'", "printf '%s' foo'one  two'"),
])
def test_ansi_c_and_concatenated_literal_contents_are_behavioral(old: str, new: str) -> None:
    """Quote masking retains literal bytes instead of splitting on their spaces."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        "Fix shell output", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("arrow", [
    '(x: number): Promise<number> => x',
    '<T>(x: T) => x',
    '<T extends Record<string, unknown>>(x: T): Promise<T> => x',
    '(x: number): (() => number) => () => x',
    '(x: number): { value: number } => ({ value: x })',
])
def test_typed_and_generic_async_arrow_newlines_are_behavioral(arrow: str) -> None:
    """Async-arrow restrictions also apply to TypeScript annotations and generics."""
    path = 'frontend/src/client.ts'
    assert delivery.pr_delivers_implementation(
        "Repair typed async arrow", [path],
        patch(path, [f'const f = async {arrow};'], ['const f = async', f'{arrow};']),
    )[0]


def test_generic_async_named_call_wrapping_remains_formatting() -> None:
    """A generic function called async is still an ordinary call, not an arrow."""
    path = 'frontend/src/client.ts'
    assert not delivery.pr_delivers_implementation(
        "Fix TS behavior", [path], patch(path, ['async<T>(value);'], ['async', '<T>(value);'])
    )[0]


@pytest.mark.parametrize("old,new", [
    ('echo ${value: 1:2}', 'echo ${value:  1:2}'),
    ('echo ${value:1+1:2}', 'echo ${value: 1 + 1 : 2}'),
    ('echo ${@:1:2}', 'echo ${@: 1 : 2}'),
    ('echo ${1:1:2}', 'echo ${1: 1 : 2}'),
    ('echo ${items[@]:1:2}', 'echo ${items[@]: 1 : 2}'),
    ('echo `printf foo`', 'echo `printf   foo`'),
    ('echo "${value: 1:2}"', 'echo "${value:  1:2}"'),
    ('echo "`printf foo`"', 'echo "`printf   foo`"'),
    ('echo "$(printf foo)"', 'echo "$(printf   foo)"'),
])
def test_executable_shell_substitution_spacing_is_not_delivery(old: str, new: str) -> None:
    """Arithmetic/subcommand formatting is not mistaken for changed literal data."""
    path = '.github/scripts/worker.sh'
    assert not delivery.pr_delivers_implementation(
        "Fix shell behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("old,new", [
    ('echo ${value:1:2}', 'echo ${value:2:2}'),
    ('echo ${value: -1}', 'echo ${value:-1}'),
    ('echo ${value:-one two}', 'echo ${value:-one  two}'),
    ('echo `printf "one two"`', 'echo `printf "one  two"`'),
    ('echo "literal  ${value}"', 'echo "literal ${value}"'),
    ('echo "${value:1:2}"', 'echo "${value:2:2}"'),
])
def test_shell_substitution_values_and_literal_spaces_remain_behavioral(old: str, new: str) -> None:
    """Normalize executable syntax without erasing offsets, defaults or literal bytes."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        "Fix shell behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


@pytest.mark.parametrize("template", [
    'cat <<EOF\n{text}\nEOF\n',
    'cat <<\'EOF\'\n{text}\nEOF\n',
    'echo "$(cat <<EOF\n{text}\nEOF\n)"',
    'echo "`cat <<EOF\n{text}\nEOF\n`"',
    'echo "$(cat <<\'EOF\'\n{text}\nEOF\n)"',
    'cat <<-EOF\n\t{text}\n\tEOF\n',
])
def test_heredoc_input_spaces_are_behavioral(template: str) -> None:
    """Heredoc stdin, including quoted command substitutions, is literal input."""
    path = '.github/scripts/worker.sh'
    old = template.format(text='one  two')
    new = template.format(text='one two')
    assert delivery.pr_delivers_implementation(
        "Repair heredoc text", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


def test_heredoc_hash_line_is_data_not_comment() -> None:
    """Comment syntax in heredoc input must not be stripped by the shell lexer."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        "Repair input text", [path], patch(
            path, ['cat <<EOF', '# old text', 'EOF'], ['cat <<EOF', '# new text', 'EOF']
        )
    )[0]


@pytest.mark.parametrize("old,new", [
    ('cat <<EOF\nvalue\nEOF\n', 'cat <<OTHER\nvalue\nOTHER\n'),
    ('cat <<EOF\nvalue\nEOF\n', 'cat <<-EOF\n\tvalue\n\tEOF\n'),
    ('cat <<EOF\n$(printf foo)\nEOF\n', 'cat <<EOF\n$(printf   foo)\nEOF\n'),
    ('cat <<EOF\none\\\ntwo\nEOF\n', 'cat <<EOF\nonetwo\nEOF\n'),
])
def test_equivalent_heredoc_formatting_is_not_delivery(old: str, new: str) -> None:
    """Delimiter names, stripped tabs and actual continuations do not alter stdin."""
    path = '.github/scripts/worker.sh'
    assert not delivery.pr_delivers_implementation(
        "Fix shell behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


def test_quoted_heredoc_substitution_text_remains_literal() -> None:
    """A quoted delimiter disables substitutions, so their internal spaces are data."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        "Repair literal input", [path], patch(
            path, ["cat <<'EOF'", '$(printf foo)', 'EOF'],
            ["cat <<'EOF'", '$(printf   foo)', 'EOF'],
        )
    )[0]


def test_quoted_shift_text_is_not_a_heredoc() -> None:
    """A redirect-looking string literal cannot start heredoc extraction."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        "Repair shell literal", [path], patch(path, ['echo "<<EOF"'], ['echo "<<OTHER"'])
    )[0]


@pytest.mark.parametrize("template", ['(( value = 1 << {shift} ))', 'echo "$((1 << {shift}))"'])
def test_arithmetic_shift_is_not_a_heredoc(template: str) -> None:
    """The << arithmetic operator cannot start heredoc input extraction."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        "Repair shell arithmetic", [path],
        patch(path, [template.format(shift=1)], [template.format(shift=2)]),
    )[0]


def test_arithmetic_shift_spacing_remains_formatting() -> None:
    """Spacing a shift operator is equivalent without being a false heredoc."""
    path = '.github/scripts/worker.sh'
    assert not delivery.pr_delivers_implementation(
        "Repair shell arithmetic", [path],
        patch(path, ['(( value = 1<<2 ))'], ['(( value = 1 << 2 ))']),
    )[0]


@pytest.mark.parametrize("old,new", [
    ('printf one && printf two', 'printf one &&\nprintf two'),
    ('printf one || printf two', 'printf one ||\nprintf two'),
    ('printf one | cat', 'printf one |\ncat'),
])
def test_shell_operator_wrapping_is_formatting(old: str, new: str) -> None:
    """The Bash grammar identifies optional operator line breaks."""
    path = '.github/scripts/worker.sh'
    assert not delivery.pr_delivers_implementation(
        "Fix shell behavior", [path], patch(path, old.splitlines(), new.splitlines())
    )[0]


def test_css_selector_function_wrapping_is_formatting() -> None:
    """Whitespace inside selector-function arguments is not a descendant combinator."""
    path = 'frontend/src/styles.css'
    assert not delivery.pr_delivers_implementation(
        "Fix CSS behavior", [path], patch(
            path, ['.a:hover:not(:disabled) {}'], ['.a:hover:not(', ' :disabled', ') {}'],
        )
    )[0]


def test_return_semicolon_wrapping_is_formatting() -> None:
    """A bare return with its semicolon on another line has the same syntax tree."""
    path = 'frontend/src/client.ts'
    assert not delivery.pr_delivers_implementation(
        "Fix JS behavior", [path], patch(path, ['return;'], ['return', ';'])
    )[0]


def test_continued_heredoc_delimiter_does_not_block_input_repair() -> None:
    """The Bash parser joins delimiter-word continuations before reading input."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        "Fix heredoc input", [path], patch(
            path, ['cat <<E\\', 'OF', 'old input', 'EOF'],
            ['cat <<E\\', 'OF', 'new input', 'EOF'],
        )
    )[0]


def test_arithmetic_shift_inside_substring_remains_behavioral() -> None:
    """A substring's arithmetic shift cannot be confused with a redirect."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        "Fix substring offset", [path], patch(
            path, ['echo ${value:1<<1:1}'], ['echo ${value:1<<2:1}'],
        )
    )[0]


def test_missing_parser_returns_actionable_retry(monkeypatch: MonkeyPatch) -> None:
    """Missing runtime dependencies never authorize a source delivery."""
    module = load_controller()
    path = '.github/scripts/worker.sh'
    monkeypatch.setattr(module, 'pr_json', lambda _pr: {'headRefOid': HEAD})
    monkeypatch.setattr(delivery, 'complete_pr_diff', lambda *_args: (
        [path], patch(path, ['echo old'], ['echo new']),
    ))

    def unavailable(_source: str) -> str:
        raise RuntimeError('install factory syntax dependencies')

    monkeypatch.setattr(delivery, '_shell_syntax', unavailable)
    result = module.implementation_delivery_gate(4046, HEAD, 'Implement behavior')
    assert result['decision'] == 'retry'
    assert 'install factory syntax dependencies' in result['reason']


def test_factory_runtime_pins_match_development_group() -> None:
    """Review and development must install identical parser versions."""
    import tomllib

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / 'pyproject.toml').read_text())
    expected = config['dependency-groups']['factory-syntax']
    actual = (root / '.github/actions/factory-syntax/requirements.txt').read_text().splitlines()
    assert sorted(expected) == sorted(line for line in actual if line and not line.startswith('#'))


@pytest.mark.parametrize('workflow', [
    'free-model-factory-run.yml', 'fixed-model-factory-dispatch.yml',
    'factory-ready-merge-drain.yml', 'factory-incident-bot-promote.yml',
    'factory-provenance-reconcile.yml', 'factory-fork-execution.yml',
    'factory-fork-persistence.yml',
])
def test_factory_control_plane_installs_parser_runtime(workflow: str) -> None:
    """Standalone control-plane jobs must provision the gate's real dependencies."""
    import yaml

    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / '.github/workflows' / workflow).read_text())
    # Jobs that only validate a fenced assignment or export Neon projections
    # never reach the delivery gate; every job that can is checked.
    gate_markers = (
        'factory-review-controller', 'factory_implementation_delivery',
        'free-model-factory-worker', 'factory-fork', 'release',
        'merge', 'promote', 'reconcile', 'dispatch',
    )
    gated = 0
    for name, job in config['jobs'].items():
        steps = job.get('steps', [])
        body = name + '\n' + '\n'.join(
            str(step.get('run', '')) + str(step.get('name', '')) for step in steps
        )
        if not any(marker in body for marker in gate_markers):
            continue
        gated += 1
        assert any(step.get('uses') == './.github/actions/factory-syntax' for step in steps), name
    assert gated


def test_async_comment_cannot_hide_restricted_newline() -> None:
    """Comments between async and its parameters cannot erase the restricted gap."""
    path = 'frontend/src/client.ts'
    assert delivery.pr_delivers_implementation(
        "Repair typed async arrow", [path], patch(
            path, ['const f = async /* note */ (x: number) => x;'],
            ['const f = async /* note */', '(x: number) => x;'],
        )
    )[0]


def test_unparsed_source_cannot_borrow_comment_evidence() -> None:
    """Recovery trees with unknown syntax do not turn prose edits into delivery."""
    path = 'frontend/src/client.ts'
    assert not delivery.pr_delivers_implementation(
        "Implement behavior", [path], patch(path, ['const = ; // old'], ['const = ; // new'])
    )[0]


@pytest.mark.parametrize('path,old,new', [
    ('frontend/src/client.ts', 'const text = "plain";', "const text = 'plain';"),
    ('frontend/src/styles.css', '.a { content: "plain"; }', ".a { content: 'plain'; }"),
    ('.github/scripts/worker.sh', 'printf "%s" "plain"', "printf '%s' 'plain'"),
])
def test_plain_quote_delimiter_formatting_is_not_delivery(path: str, old: str, new: str) -> None:
    """Equivalent quote choices around plain literals are formatting."""
    assert not delivery.pr_delivers_implementation(
        'Implement behavior', [path], patch(path, [old], [new])
    )[0]


def test_shell_shebang_is_executable_configuration() -> None:
    """A shebang chooses the interpreter; it is not an ordinary comment."""
    path = '.github/scripts/worker.sh'
    assert delivery.pr_delivers_implementation(
        'Repair interpreter selection', [path], patch(
            path, ['#!/bin/sh', 'echo ready'], ['#!/bin/bash', 'echo ready'],
        )
    )[0]


@pytest.mark.parametrize('path,broken,fixed', [
    ('.github/scripts/worker.sh', 'printf ready &&', 'printf ready'),
    ('app/feature.py', 'def broken(:', 'def fixed():\n    return True'),
])
def test_syntax_repair_to_parsed_source_is_delivery(path: str, broken: str, fixed: str) -> None:
    """A parser must allow a repair even when the original source did not parse."""
    assert delivery.pr_delivers_implementation(
        'Repair invalid syntax', [path], patch(path, broken.splitlines(), fixed.splitlines())
    )[0]
    assert not delivery.pr_delivers_implementation(
        'Implement behavior', [path], patch(path, fixed.splitlines(), broken.splitlines())
    )[0]


def test_factory_parser_runtime_uses_application_python() -> None:
    """Python AST inspection must understand the application's Python version."""
    import yaml

    root = Path(__file__).resolve().parents[1]
    action = yaml.safe_load((root / '.github/actions/factory-syntax/action.yml').read_text())
    assert any(
        step.get('uses') == 'actions/setup-python@v7'
        and step.get('with', {}).get('python-version') == '3.14'
        for step in action['runs']['steps']
    )


@pytest.mark.parametrize("suffix", ["js", "jsx", "ts", "tsx", "mjs", "cjs", "css", "scss"])
def test_new_comment_only_source_is_not_delivery(suffix: str) -> None:
    """A new file containing only ignored comments has no implementation."""
    path = f"frontend/src/new_feature.{suffix}"
    assert not delivery.pr_delivers_implementation(
        "Implement new behavior", [path], patch(path, [], ["/* documentation only */"])
    )[0]


def test_sql_operator_boundary_repair_is_delivery() -> None:
    """Joining separate operators repairs syntax rather than formatting it."""
    path = "app/query.sql"
    assert delivery.pr_delivers_implementation(
        "Repair SQL concatenation", [path], patch(
            path, ["SELECT left_value | | right_value FROM records;"],
            ["SELECT left_value || right_value FROM records;"],
        )
    )[0]
    assert not delivery.pr_delivers_implementation(
        "Repair SQL concatenation", [path], patch(
            path, ["SELECT left_value||right_value FROM records;"],
            ["SELECT left_value || right_value FROM records;"],
        )
    )[0]


@pytest.mark.parametrize("old,new", [
    ("limit=-1", "limit = -1"),
    ("limit=+1", "limit = +1"),
])
def test_assignment_sign_spacing_is_not_delivery(old: str, new: str) -> None:
    """Assignment and a neighboring unary sign are separate operators."""
    path = "app/settings.toml"
    assert not delivery.pr_delivers_implementation(
        "Implement configuration", [path], patch(path, [old], [new])
    )[0]


def test_mid_file_change_in_40_line_file_counts_as_delivery() -> None:
    """A change at line 25 of a 40-line file must count as implementation delivery.

    Regression test for n=3 context regression: with n=3 the diff hunk starts at
    line 22 (@@ -22,7 +22,7 @@) which fails _has_code_delta's complete-file
    context requirement (must start at line 0 or 1). Full context produces
    @@ -1,40 +1,40 @@ which passes.
    """
    path = "app/services/mid_file_change.py"
    # Valid Python: a class with 20 methods (each method = 2 lines), change method 13
    # Total lines: 1 (class) + 20*2 = 41 lines
    old = ["class Service:"]
    for i in range(1, 21):
        old.append(f"    def method_{i}(self) -> int:")
        old.append(f"        return {i}")
    new = old.copy()
    # Change method_13's return value (line 25 in 1-indexed: class=1, methods 1-12 = 24 lines, method_13 def=25, return=26)
    new[26] = "        return 999"  # 0-indexed: return statement of method_13
    assert delivery.pr_delivers_implementation(
        "Fix mid-file behavior", [path], patch(path, old, new)
    )[0]


def test_multi_hunk_change_first_and_last_lines_counts_as_delivery() -> None:
    """Edits near first and last lines of a long file must count as delivery.

    With n=3 this produces two hunks (@@ -2,7 +2,7 @@ and @@ -45,6 +45,6 @@)
    which fails _has_code_delta's single-hunk requirement. Full context produces
    a single @@ -1,50 +1,50 @@ hunk which passes.
    """
    path = "app/services/multi_hunk_change.py"
    # Valid Python: a class with 25 methods (each method = 2 lines), change method 2 and method 24
    # Total lines: 1 (class) + 25*2 = 51 lines
    old = ["class Service:"]
    for i in range(1, 26):
        old.append(f"    def method_{i}(self) -> int:")
        old.append(f"        return {i}")
    new = old.copy()
    new[3] = "        return 999"    # method_2 return (0-indexed: class=0, method_1 def=1, method_1 return=2, method_2 def=3, method_2 return=4)
    new[48] = "        return 888"   # method_24 return (near end)
    assert delivery.pr_delivers_implementation(
        "Fix multi-location behavior", [path], patch(path, old, new)
    )[0]


def test_renamed_file_through_merge_base_fallback_counts_as_delivery() -> None:
    """A renamed file using previous_filename through the merge-base path counts as delivery.

    This tests the omitted-patch fallback path for renamed files (status=renamed
    with previous_filename) which uses _unified_file_diff with base content from
    the merge-base commit.
    """
    import json

    renamed_from = "app/services/old_reader.py"
    renamed_to = "app/services/new_reader.py"
    old_content = "OLD_VALUE = 1\n"
    new_content = "NEW_VALUE = 2\n"

    def read(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/pulls/44"):
            return json.dumps({
                "head": {"sha": "a" * 40}, "base": {"sha": "base"},
            })
        if "/compare/" in endpoint:
            return json.dumps({"merge_base_commit": {"sha": "ancestor"}})
        if endpoint.endswith("/files"):
            return json.dumps([[{
                "filename": renamed_to,
                "status": "renamed",
                "previous_filename": renamed_from,
                "changes": 2,
            }]])
        if f"ref={'a' * 40}" in endpoint:
            return new_content
        if "ref=ancestor" in endpoint:
            return old_content
        raise AssertionError(f"unexpected read {endpoint}")

    paths, diff = delivery.complete_pr_diff(44, "a" * 40, "owner/repo", read)

    # Verify the diff contains the rename header and complete-file context
    assert renamed_to in paths
    assert f"diff --git a/{renamed_to} b/{renamed_to}" in diff
    # Full context: single hunk starting at line 1
    assert "@@ -1,1 +1,1 @@" in diff or "@@ -1 +1 @@" in diff
    assert "-OLD_VALUE = 1" in diff
    assert "+NEW_VALUE = 2" in diff
    assert delivery.pr_delivers_implementation("Rename and fix reader", paths, diff)[0]


def test_complete_file_context_single_hunk_starting_at_line_one() -> None:
    """_unified_file_diff must produce a single hunk starting at line 1/0.

    This assertion locks the complete-file context behavior that _has_code_delta
    requires. Any change to _unified_file_diff that produces multiple hunks or
    a hunk not starting at line 1 (old) / 1 (new) or 0 (old) / 1 (new) will
    fail this test.
    """
    import re

    # Test various file sizes and change positions
    test_cases = [
        # (old_lines, new_lines, description)
        (["line 1"], ["changed line 1"], "single line change"),
        (["line 1", "line 2"], ["line 1", "changed line 2"], "two lines, change second"),
        ([f"line {i}" for i in range(1, 41)], [f"line {i}" if i != 25 else "changed line 25" for i in range(1, 41)], "40 lines, change at line 25"),
        ([f"line {i}" for i in range(1, 101)], [f"line {i}" if i not in (5, 95) else f"changed line {i}" for i in range(1, 101)], "100 lines, changes at lines 5 and 95"),
        ([], ["added line"], "empty to one line"),
        (["removed line"], [], "one line to empty"),
        (["line 1", "line 2"], ["line 1", "line 2", "added line 3"], "add line at end"),
        (["line 1", "line 2", "line 3"], ["line 1", "line 2"], "remove line at end"),
    ]

    hunk_header_pattern = re.compile(r"^@@ -([01])(?:,(\d+))? \+([01])(?:,(\d+))? @@")

    for old_lines, new_lines, desc in test_cases:
        old = "\n".join(old_lines)
        new = "\n".join(new_lines)
        diff = delivery._unified_file_diff("test/path.py", old, new)

        # Parse hunks
        hunks = []
        for line in diff.splitlines():
            match = hunk_header_pattern.match(line)
            if match:
                old_start = int(match.group(1))
                new_start = int(match.group(3))
                hunks.append((old_start, new_start))

        # Must have exactly one hunk
        assert len(hunks) == 1, f"{desc}: expected 1 hunk, got {len(hunks)}: {hunks}"

        # Hunk must start at line 0 or 1 for both old and new
        old_start, new_start = hunks[0]
        assert old_start in (0, 1), f"{desc}: old hunk starts at line {old_start}, expected 0 or 1"
        assert new_start in (0, 1), f"{desc}: new hunk starts at line {new_start}, expected 0 or 1"

        # For non-empty files, should start at line 1
        if old_lines:
            assert old_start == 1, f"{desc}: non-empty old file should start at line 1, got {old_start}"
        if new_lines:
            assert new_start == 1, f"{desc}: non-empty new file should start at line 1, got {new_start}"
