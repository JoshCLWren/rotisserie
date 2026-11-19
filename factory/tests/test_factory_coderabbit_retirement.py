"""CodeRabbit reviews are requested but stay advisory for factory gates and prompts."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / ".github/scripts/factory-review-controller.py"
RUNNER = ROOT / ".github/workflows/factory-ready-merge-drain.yml"
LEGACY_REVIEW = ROOT / ".github/scripts/hourly_coderabbit_review.py"
HEAD = "a" * 40


def controller_module():
    spec = importlib.util.spec_from_file_location("retired_rabbit_review_controller", CONTROLLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _thread(login: str) -> dict:
    return {
        "isResolved": False,
        "comments": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"author": {"login": login}, "commit": {"oid": HEAD}}],
        },
    }


def test_exact_head_coderabbit_thread_is_ignored_but_human_thread_blocks() -> None:
    gate = controller_module().interpret_review_threads
    assert gate([_thread("coderabbitai[bot]")], head=HEAD)["decision"] == "pass"
    assert gate([_thread("coderabbitai")], head=HEAD)["decision"] == "pass"
    assert gate([_thread("human-reviewer")], head=HEAD)["decision"] == "deny"
    assert gate([_thread("coderabbitai[bot]"), _thread("human-reviewer")], head=HEAD)["decision"] == "deny"


def test_retired_bot_rest_comments_do_not_require_graphql() -> None:
    gate = controller_module().review_thread_query_required
    only_bot = [[{"user": {"login": "coderabbitai[bot]"}, "commit_id": HEAD}]]
    human = [[{"user": {"login": "other-reviewer"}, "commit_id": HEAD}]]
    assert gate(only_bot, head=HEAD)["decision"] == "pass"
    assert gate(human, head=HEAD) is None


def test_merge_drain_requests_coderabbit_reviews_without_blocking_merges() -> None:
    """Review requests run on the drain cadence; failures never block merges.

    Requests were restored after #5209 removed them; findings remain advisory
    for the factory merge gate (see the tests above).
    """
    drain = (ROOT / ".github/workflows/factory-ready-merge-drain.yml").read_text(encoding="utf-8")
    step_name = "      - name: Ask Jev for CodeRabbit review on the existing drain cadence"
    assert drain.count(step_name) == 1
    step = drain.split(step_name, 1)[1].split("\n      - name:", 1)[0]
    assert "continue-on-error: true" in step
    assert 'python3 .github/scripts/hourly_coderabbit_review.py \\\n            --repo "$GITHUB_REPOSITORY"' in step


def test_coderabbit_request_cli_is_live_and_validates_repository() -> None:
    result = subprocess.run(
        [sys.executable, str(LEGACY_REVIEW), "--repo", "not a repo"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode != 0
    assert "invalid repository name" in result.stderr
    assert "retired" not in (result.stdout + result.stderr).lower()
