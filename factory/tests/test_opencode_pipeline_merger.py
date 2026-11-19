"""Tests for the opencode pipeline merger script validation."""

import re

import pytest


_WT_PATH_BLOCK_RE = re.compile(
    r'git -C "\$wt_path".*?rebase origin/main.*?'
    r'git -C "\$wt_path".*?merge origin/main --no-edit -X theirs',
    re.DOTALL,
)


_TMPWT_PATH_BLOCK_RE = re.compile(
    r'git -C "\$tmpwt".*?rebase origin/main.*?'
    r'git -C "\$tmpwt".*?merge origin/main --no-edit -X theirs',
    re.DOTALL,
)


_REBASE_CONDITION = re.compile(r'\$STRATEGY"\s*==\s*"rebase"')


@pytest.fixture(name="pipeline_script")
def _pipeline_script():
    import pathlib

    return pathlib.Path("scripts/opencode_pipeline.sh").read_text()


def test_rebranch_on_rebase_strategy(pipeline_script: str) -> None:
    """When STRATEGY=rebase the script must call git rebase, not merge."""
    assert _REBASE_CONDITION.search(pipeline_script) is not None, (
        "The merger script must contain a STRATEGY==rebase branch"
    )
    block = _WT_PATH_BLOCK_RE.search(pipeline_script)
    assert block is not None, (
        "Could not find the rebase-then-merge fallback block for the worktree path"
    )
    rebase_pos = pipeline_script.find("rebase origin/main", block.start())
    merge_pos = pipeline_script.find("merge origin/main --no-edit -X theirs", block.start())
    assert rebase_pos != -1, "rebase origin/main missing from the worktree block"
    assert merge_pos != -1, "merge origin/main --no-edit -X theirs missing from the worktree block"
    assert rebase_pos < merge_pos, (
        "rebase origin/main must appear before merge origin/main --no-edit -X theirs "
        "in the worktree block to ensure rebase runs when STRATEGY=rebase"
    )
    assert "rebase origin/main -X theirs" in pipeline_script[rebase_pos : rebase_pos + 80], (
        "The worktree rebase path must include -X theirs so conflicting PRs are "
        "auto-resolved instead of aborting the rebase"
    )


def test_rebranch_on_scratch_worktree(pipeline_script: str) -> None:
    """When STRATEGY=rebase the script must call git rebase for scratch worktrees."""
    block = _TMPWT_PATH_BLOCK_RE.search(pipeline_script)
    assert block is not None, (
        "Could not find the rebase-then-merge fallback block for the scratch worktree"
    )
    rebase_pos = pipeline_script.find("rebase origin/main", block.start())
    merge_pos = pipeline_script.find("merge origin/main --no-edit -X theirs", block.start())
    assert rebase_pos != -1, "rebase origin/main missing from the scratch worktree block"
    assert merge_pos != -1, (
        "merge origin/main --no-edit -X theirs missing from the scratch worktree block"
    )
    assert rebase_pos < merge_pos, (
        "rebase origin/main must appear before merge origin/main --no-edit -X theirs "
        "in the scratch worktree block"
    )
    assert "rebase origin/main -X theirs" in pipeline_script[rebase_pos : rebase_pos + 80], (
        "The scratch worktree rebase path must include -X theirs so conflicting PRs are "
        "auto-resolved instead of aborting the rebase"
    )
    assert 'log_warn "merger — rebase failed for $branch (scratch)"' in pipeline_script, (
        "The rebase-failure log message must be distinguishable from the non-rebase "
        "merge-failure path so operators can tell which strategy failed"
    )