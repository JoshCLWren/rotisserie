"""Regression coverage for the trusted local Ollama review protocol."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/local_review_contract.py"
SPEC = importlib.util.spec_from_file_location("local_review_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
contract = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contract
SPEC.loader.exec_module(contract)

HEAD = "a" * 40


def target(*, state: str = "open", draft: bool = False,
           head: str = HEAD, body: str = "Closes #4964.",
           labels: list[dict[str, str]] | None = None) -> dict:
    """Build an exact-head PR fixture with the requested state."""
    return {
        "state": state,
        "draft": draft,
        "head": {"sha": head, "ref": "fix/4964-ollama-review-json-contract"},
        "body": body,
        "labels": labels or [{"name": "factory:unowned"}],
    }


def response(content: str, finish: str = "stop") -> dict:
    """Build an OpenAI-compatible chat response fixture."""
    return {
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": {"completion_tokens": 1400},
    }


def test_target_uses_canonical_link_and_preserves_exact_head() -> None:
    """Resolve only authoritative issue references at an unchanged PR head."""
    assert contract.target(target(), expected_head=HEAD) == {
        "head": HEAD, "issue": 4964, "non_closing": False
    }
    part = target(body="Part of #4847.")
    assert contract.target(part)["non_closing"] is True


@pytest.mark.parametrize(
    "pr, expected",
    [
        (target(state="closed"), "not an open"),
        (target(draft=True), "not an open"),
        (target(head="b" * 40), "head changed"),
        (target(body="A PR without an issue link"), "no canonical linked issue"),
        (target(labels=[{"name": "factory:41"}]), "active factory lease"),
    ],
)
def test_closed_stale_unlinked_or_claimed_target_is_never_reviewed(
    pr: dict, expected: str
) -> None:
    """Closed, moved, unlinked or actively leased PRs must fail closed."""
    with pytest.raises(contract.ReviewContractError, match=expected):
        contract.target(pr, expected_head=HEAD if pr["head"]["sha"] != HEAD else None)


def test_openai_compatible_json_mode_has_rubric_aware_output_budget() -> None:
    """Use bounded output size, deterministic inference and JSON mode."""
    request = contract.make_request("qwen2.5-coder:7b", "Review actual diff", 12)
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0
    assert request["stream"] is False
    assert request["max_tokens"] == 4200
    assert contract.make_request("qwen2.5-coder:7b", "x", 1)["max_tokens"] == 4096
    assert contract.make_request("qwen2.5-coder:7b", "x", 100)["max_tokens"] == 8192


def test_complete_json_approval_is_parsed_without_inventing_acceptance() -> None:
    """Parse complete structured evidence without manufacturing missing items."""
    content = (
        '{"verdict":"APPROVE","reason":"All inspected changes satisfy the linked issue.",'
        '"acceptance":{"issue":4964,"items":[]}}'
    )
    parsed = contract.parse_response(response(content))
    assert parsed["verdict"] == "APPROVE"
    assert parsed["acceptance"]["issue"] == 4964


def test_bounded_truncation_is_not_silently_salvaged() -> None:
    """Reject shortened and invalid responses before any trusted handoff."""
    bad = response('{"verdict":"APPROVE","reason":"unfinished"', finish="length")
    with pytest.raises(contract.ReviewContractError, match="token limit"):
        contract.parse_response(bad)
    with pytest.raises(contract.ReviewContractError, match="malformed review JSON"):
        contract.parse_response(response('{"verdict":"APPROVE","reason":'))


def test_reject_requires_actionable_reason_and_cannot_fake_approval() -> None:
    """Require substantive findings or a structured approval payload."""
    with pytest.raises(contract.ReviewContractError, match="substantive review reason"):
        contract.parse_response(response('{"verdict":"REJECT","reason":"No"}'))
    parsed = contract.parse_response(response(
        '{"verdict":"REJECT","reason":"tests/test_example.py does not verify expected behavior."}'
    ))
    assert parsed["verdict"] == "REJECT"
    with pytest.raises(contract.ReviewContractError, match="structured issue acceptance"):
        contract.parse_response(response(
            '{"verdict":"APPROVE","reason":"All inspected changes satisfy the linked issue."}'
        ))


def test_script_uses_controller_and_never_claims_truncated_or_stale_review() -> None:
    """Keep exact-head and merge authorization behind the trusted controller."""
    script = (ROOT / ".github/scripts/local-ollama-review.sh").read_text()
    workflow = (ROOT / ".github/workflows/local-ollama-review.yml").read_text()
    assert 'ISSUE="4825"' not in script
    assert 'local_review_contract.py target' in script
    assert '--expected-head "$HEAD"' in script
    assert 'local_review_contract.py response /tmp/resp.json' in script
    assert 'controller_verdict=repair' in script
    assert 'jq \'.max_tokens = 8192\'' in script
    assert 'gh_retry gh workflow run factory-ready-merge-drain.yml' in script
    assert 'gh pr merge' not in script
    assert 'timeout-minutes: 180' in workflow
    assert '--connect-timeout 60 --max-time 7200' in script
    assert "--write-out 'http=%{http_code} connect_s=%{time_connect}" in script
    assert 'first_byte_s=%{time_starttransfer} total_s=%{time_total}' in script
    assert '[local-review-timing]' in script
    assert 'curl_exit=$?' in script
    assert 'GITHUB_STEP_SUMMARY' in script
    assert 'http=524' in script
    assert '--output /tmp/resp.json' in script


def test_review_shell_has_valid_bash_syntax() -> None:
    """Ensure the shell pipeline parses before it reaches a live worker."""
    import subprocess

    result = subprocess.run(
        ["bash", "-n", str(ROOT / ".github/scripts/local-ollama-review.sh")],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("scope", "expected", "should_succeed"),
    [
        ({"non_closing": False}, "false", True),
        ({"non_closing": True}, "true", True),
        ({}, "", False),
        ({"non_closing": None}, "", False),
        ({"non_closing": "false"}, "", False),
    ],
)
def test_review_shell_accepts_explicit_false_scope_under_errexit(
    scope: dict, expected: str, should_succeed: bool
) -> None:
    """Prevent jq -e from dropping normal closing-linked PRs before inference."""
    import json
    import shutil
    import subprocess

    script = (ROOT / ".github/scripts/local-ollama-review.sh").read_text()
    assignment = next(
        line for line in script.splitlines() if line.startswith("NON_CLOSING=")
    )
    assert 'then tostring else error(' in assignment
    if not shutil.which("jq"):
        pytest.skip("jq is not installed in this test environment")
    command = (
        "target=\"$1\"\n"
        + assignment + "\n"
        + 'printf "%s" "$NON_CLOSING"\n'
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", command, "review-test", json.dumps(scope)],
        text=True, capture_output=True, check=False,
    )
    if should_succeed:
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected
    else:
        assert result.returncode != 0


def test_patch_excerpt_preserves_short_complete_diff() -> None:
    """Small patches retain their entire original diff and context."""
    patch = (
        "diff --git a/app.py b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )
    assert contract.review_patch_excerpt(patch) == patch


def test_review_patch_keeps_late_worker_lease_test_after_large_context() -> None:
    """Late worker tests survive oversized earlier-file context."""
    patch = (
        "diff --git a/src/large.py b/src/large.py\n"
        "@@ -1,1000 +1,1000 @@\n"
        + (" unchanged context that should not consume model tokens\n" * 450)
        + "+def dispatch(worker): return worker\n"
        + "diff --git a/tests/test_factory_work_controller_runtime.py "
        "b/tests/test_factory_work_controller_runtime.py\n"
        "@@ -1 +1,4 @@\n"
        "+def test_three_digit_worker_104_lease_write():\n"
        "+    assert lease_owner(104) == 'factory:104'\n"
    )
    assert len(patch) > 16_000
    excerpt = contract.review_patch_excerpt(patch, 14_000)
    assert len(excerpt) <= 14_000
    assert "diff --git a/src/large.py" in excerpt
    assert "diff --git a/tests/test_factory_work_controller_runtime.py" in excerpt
    assert "test_three_digit_worker_104_lease_write" in excerpt
    assert "factory:104" in excerpt
    assert "DIFF EXCERPT" in excerpt


def test_review_patch_balances_changed_line_heavy_files_without_losing_tail() -> None:
    """Budgeting covers each file even with many added source lines."""
    files = [
        (
            f"diff --git a/src/f{i}.py b/src/f{i}.py\n"
            "@@ -1 +1,1200 @@\n"
            + "".join(f"+changed_line_{i}_{j} = True\n" for j in range(180))
        )
        for i in range(6)
    ]
    files.append(
        "diff --git a/tests/test_roster.py b/tests/test_roster.py\n"
        "@@ -1 +1 @@\n"
        "+def test_worker_107_can_claim(): assert True\n"
    )
    excerpt = contract.review_patch_excerpt("".join(files), 3000)
    assert len(excerpt) <= 3000
    for i in range(6):
        assert f"diff --git a/src/f{i}.py" in excerpt
    assert "diff --git a/tests/test_roster.py" in excerpt
    assert "test_worker_107_can_claim" in excerpt
    assert "other changed lines from this file omitted" in excerpt


def test_local_review_shell_uses_balanced_full_diff_instead_of_prefix_truncation() -> None:
    """The trusted review shell calls the bounded across-file projector."""
    shell = (ROOT / ".github/scripts/local-ollama-review.sh").read_text()
    assert "truncate -s 16000 /tmp/p.patch" not in shell
    assert 'contract.review_patch_excerpt(open("/tmp/p.patch"' in shell
    assert 'read()[:14000]' not in shell



def test_patch_excerpt_preserves_mode_only_metadata_when_oversized() -> None:
    """A permission-only change remains visible after stripping context."""
    patch = (
        "diff --git a/scripts/reviewer.sh b/scripts/reviewer.sh\n"
        "old mode 100755\n"
        "new mode 100644\n"
        "diff --git a/src/large.py b/src/large.py\n"
        "@@ -1 +1 @@\n"
        + (" irrelevant unchanged context\n" * 100)
        + "+fixed = True\n"
    )
    result = contract.review_patch_excerpt(patch, 700)
    assert "old mode 100755" in result
    assert "new mode 100644" in result
    assert "diff --git a/src/large.py" in result


def test_patch_excerpt_keeps_whole_changed_lines_or_fails_closed() -> None:
    """No semantic reviewer receives a severed changed line as evidence."""
    patch = (
        "diff --git a/src/f.py b/src/f.py\n"
        "@@ -1 +1,100 @@\n"
        + "".join(f"+changed_value_{i} = {i}\n" for i in range(100))
        + "diff --git a/tests/test_f.py b/tests/test_f.py\n"
        "@@ -1 +1 @@\n"
        "+def test_last_worker_107(): assert True\n"
    )
    result = contract.review_patch_excerpt(patch, 700)
    originals = set(patch.splitlines())
    for line in result.splitlines():
        if line.startswith(("+", "-")):
            assert line in originals
    assert "+def test_last_worker_107(): assert True" in result
    assert "other changed lines" in result


def test_review_excerpt_rejects_many_files_when_minimum_evidence_wont_fit() -> None:
    """Fail closed rather than dropping a file from a tight review budget."""
    patch = "".join(
        f"diff --git a/very_long_path_{i}_some_test_name.py "
        f"b/very_long_path_{i}_some_test_name.py\n"
        "@@ -1 +1 @@\n"
        f"+value_{i} = True\n"
        for i in range(12)
    )
    with pytest.raises(contract.ReviewContractError, match="complete evidence"):
        contract.review_patch_excerpt(patch, 256)



def test_local_review_prompt_requires_substantive_reason_for_approve_and_reject() -> None:
    """The model prompt explicitly meets the strict response validator."""
    shell = (ROOT / ".github/scripts/local-ollama-review.sh").read_text()
    assert "For BOTH APPROVE and REJECT, include a reason" in shell
    assert '"string of at least 30 characters explaining concrete, independently checked evidence; "' in shell
    assert 'contract.review_patch_excerpt(open("/tmp/p.patch"' in shell

def test_review_candidate_skips_unlinked_older_pr_without_stealing_leases() -> None:
    """Select the oldest actually reviewable PR using canonical issue semantics."""
    labels = [{"name": "factory:review"}, {"name": "factory:unowned"}]
    candidates = [
        {"number": 7, "isDraft": False, "labels": labels,
         "headRefName": "misc/no-link", "body": "Part of #4737 / #4335."},
        {"number": 9, "isDraft": False, "labels": labels,
         "headRefName": "fix/4978-eligible", "body": "Closes #4978."},
        {"number": 8, "isDraft": False, "labels": labels + [{"name": "factory:77"}],
         "headRefName": "fix/4978-owned", "body": "Closes #4978."},
        {"number": 10, "isDraft": False, "labels": labels,
         "headRefName": "misc/non-closing", "body": "Part of #4737."},
    ]
    assert contract.select_review_candidate(candidates) == 9
    assert contract.select_review_candidate(candidates[:1]) is None
    assert contract.select_review_candidate(candidates[3:]) == 10
