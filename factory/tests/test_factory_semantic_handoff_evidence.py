"""CI-discovered regressions for #3271 semantic review handoff evidence."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_controller() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "factory_review_handoff_regression", SCRIPTS / "factory-review-controller.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_large_diff_never_replaces_actionable_review_finding() -> None:
    controller = load_controller()
    finding = "Missing authorization check exposes private creator metadata to another user."
    diff = "diff --git a/app.py b/app.py\n" + "+    return unsafe_code()\n" * 4000
    review_log = (
        finding + "\nFACTORY_GATE_BLOCKED\n"
        + controller.AUTHORITATIVE_DIFF_EVIDENCE
        + "\ngh pr diff 3200\n" + diff
    )

    excerpt = controller.semantic_review_excerpt(review_log)

    assert finding in excerpt
    assert "gh pr diff 3200" in excerpt
    assert "+    return unsafe_code()" not in excerpt
    assert controller.has_actionable_review_findings(excerpt)


@pytest.mark.parametrize(
    "review_log",
    [
        "FACTORY_GATE_BLOCKED",
        "semantic blockers remain\nthe pr is returning to repair",
        "# widgets-factory-authoritative-diff-evidence\ngh pr diff 3200",
        "diff --git a/app.py b/app.py\n+++ b/app.py\n+const result = unsafe();",
        "```python\ndef broken():\n    return unsafe()\n```",
    ],
)
def test_source_diff_and_verdict_tokens_are_not_repair_findings(review_log: str) -> None:
    controller = load_controller()
    assert not controller.has_actionable_review_findings(review_log)



def test_factory_60_tool_transcript_is_not_actionable_repair_evidence() -> None:
    """PR #4923 was incorrectly returned to repair by command-only output."""
    controller = load_controller()
    transcript = (
        "> build · cohere/north-mini-code:free\n"
        "→ Read AGENTS.md\n"
        "→ Read docs/ISSUE_EXECUTION_PROTOCOL.md\n"
        "$ git log --oneline -10\n"
        "80b649f31 test(factory): sandboxed fork review uses composite key\n"
        "$ git show 80b649f31 --stat\n"
        "commit 80b649f31a7c2e0461b65024b26f2ef3d6d389b9\n"
        "Author: JoshCLWren\n"
        "Date: Mon Nov 9 19:47:59 2026 -0600\n"
        "tests/test_factory_fork_control.py | 49 ++++++++++++++++++\n"
        "1 file changed, 49 insertions(+)\n"
        "$ git status\n"
        "On branch fix/4922-openrouter-composite-semantic-reviews\n"
        "nothing to commit, working tree clean\n"
        "$ git diff HEAD~1 HEAD\n"
        "FACTORY_GATE_NOT_READY\n"
        "# widgets-factory-authoritative-diff-evidence\n"
        "gh pr diff 4923\n"
    )
    assert not controller.has_actionable_review_findings(transcript)


def test_substantive_review_finding_survives_tool_transcript_filter() -> None:
    controller = load_controller()
    transcript = (
        "> build · model/free\n"
        "$ git diff --stat\n"
        "Finding: The review worker can write to a contributor fork because "
        "the OpenRouter sandbox inherited GH_TOKEN; remove that credential "
        "and add a regression test for fork isolation.\n"
        "FACTORY_GATE_NOT_READY\n"
    )
    assert controller.has_actionable_review_findings(transcript)


def test_trusted_ollama_review_wakes_canonical_merge_only_when_ready() -> None:
    """A valid Qwen review should not sit until the next factory schedule."""
    reviewer = (SCRIPTS / "local-ollama-review.sh").read_text()
    workflow = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "workflows"
        / "local-ollama-review.yml"
    ).read_text()

    assert 'review_result="$(python3 .github/scripts/factory-review-controller.py review' in reviewer
    assert """jq -e --arg head "$HEAD" '.status == "ready" and .head == $head'""" in reviewer
    assert 'gh_retry gh workflow run factory-ready-merge-drain.yml' in reviewer
    assert "gh pr merge" not in reviewer
    assert "  actions: write" in workflow


def test_worker_proves_diff_inspection_without_append_of_diff_hunks() -> None:
    worker = (SCRIPTS / "free-model-factory-worker.sh").read_text()
    assert 'if gh pr diff "$NUMBER" >/dev/null; then' in worker
    assert 'gh pr diff "$NUMBER" 2>/dev/null | head -n 4000' not in worker
    assert "printf '\\n# %s-factory-authoritative-diff-evidence\\n' \"${FACTORY_MARKER_PREFIX:?}\"" in worker


def test_worker_falls_back_to_the_file_listing_when_the_diff_is_too_large() -> None:
    worker = (SCRIPTS / "free-model-factory-worker.sh").read_text()
    start = worker.index("diff_evidence=''")
    block = worker[start:worker.index("} >> \"$sanitized_review_log\"", start)]
    assert block.index('gh pr diff "$NUMBER"') < block.index("/pulls/${NUMBER}/files")
    assert block.count('gh pr diff "$NUMBER"') == 1
    assert 'printf \'%s\\n\' "$diff_evidence"' in block


def test_narration_and_tool_output_are_not_repair_findings() -> None:
    """PR #5684 went to repair on a verdict holding only these lines."""
    controller = load_controller()
    transcript = (
        "I'll start by reading the AGENTS.md file and understanding the PR context.\n"
        "Now let me examine the PR #5684 to understand what changes are in this PR.\n"
        "> build · poolside/laguna-xs-2.1\n"
        "$ git log -3\n"
        "(no output)\n"
        "Let me check the PR #5684 on GitHub to understand what changes are in it.\n"
        "commit 3fe2c38166de7e2b20a00d8642cddd99d06cd646\n"
        "Author: opencode-free-model-factory-1236998703301[bot]\n"
        "    factory: advance PR #5684 with nvidia · z-ai/glm-5.3-flash\n"
        "    checkpoint(factory): worker 1236998703301 run 45066726022 pr #5684\n"
        "    Model: z-ai/glm-5.3-flash\n"
        "    Source-branch: factory/work-checkpoints/issue-5592/iceman/45050543683\n"
        "native/roll-service/src/comicvine_v2.rs\n"
        "native/roll-service/tests/comicvine_v2_http.rs\n"
        "gh pr diff 5684\n"
        "Rust-default API ingress (fragment-derived)\tpass\t17s\thttps://github.com/o/r/actions/runs/1\n"
        '[{"bucket":"pass","completedAt":"2026-11-22T06:32:39Z","description":""}]\n'
        ' lease was released for independent review.\\n\\nWorker: opencode-free\n'
        "- Let me check the PR on GitHub to understand the changes.\n"
        "2. Now I'll look at the test file for the new behavior.\n"
        '"state": "SUCCESS", "name": "CI Summary"\n'
    )
    assert not controller.has_actionable_review_findings(transcript)


@pytest.mark.parametrize(
    "finding",
    [
        "The new retry loop never resets its counter, so the second failure aborts the job.",
        "  - The handler returns 200 before the write commits, losing the update on error.",
        "    1. The migration drops the index the new query depends on.",
        "[P1] The retry counter never resets after a successful attempt.",
        "The parser treats the literal \\n in titles as a line break, splitting the name.",
        '"Fixed" is posted before the push lands, so the reviewer sees the old head.',
    ],
)
def test_real_findings_survive_the_narration_filter(finding: str) -> None:
    controller = load_controller()
    transcript = "> build · model/free\n$ git diff --stat\n" + finding + "\nFACTORY_GATE_NOT_READY\n"
    assert controller.has_actionable_review_findings(transcript)
