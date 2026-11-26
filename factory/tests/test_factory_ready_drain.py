"""Regression coverage for the factory ready-drain and stale-head cancellation."""

from pathlib import Path

from tests.workflow_files import adopter_view


def test_dispatcher_drains_ready_factory_prs_with_exact_head_gates() -> None:
    """Ready PRs are drained only after exact-head merge gates pass."""
    dispatcher = adopter_view("free-model-factory-dispatch.yml")

    assert 'Drain exact-head ready factory PRs' in dispatcher
    assert "--label 'factory:ready'" in dispatcher
    assert 'checks: read' in dispatcher
    assert 'contents: write' in dispatcher
    assert 'current_head_review_blockers' in dispatcher
    assert '.state == "CHANGES_REQUESTED" and .commit_id == $head' in dispatcher
    assert 'reviewThreads(first:100)' in dispatcher
    assert 'gh pr checks "$pr" --repo "$GITHUB_REPOSITORY" --required' in dispatcher
    assert '[[ "$head" == "$expected_head" ]]' in dispatcher
    assert '--merge --match-head-commit "$head"' in dispatcher


def test_ready_drain_reuses_dispatch_runner_without_blocking_dispatch() -> None:
    """The drain reuses the dispatcher runner and cannot block dispatch."""
    dispatcher = adopter_view("free-model-factory-dispatch.yml")

    assert '\n  dispatch:\n' in dispatcher
    assert '\n  drain-ready:\n' not in dispatcher
    assert 'continue-on-error: true' in dispatcher
    assert dispatcher.index('Drain exact-head ready factory PRs') < dispatcher.index('Resolve and dispatch fixed workers')


def test_repair_guard_cancels_superseded_pr_heads() -> None:
    """Repair-guard runs collapse to the latest PR head."""
    guard = adopter_view("fixed-model-pr-repair-guard.yml")

    assert 'group: fixed-model-pr-repair-guard-${{ github.event.pull_request.number || github.ref }}' in guard
    assert 'cancel-in-progress: true' in guard


def test_a_refused_merge_asks_github_why_and_says_so_once_per_head() -> None:
    """gh's own merge-state check refused #5848 for hours and never asked GitHub why."""
    drain = adopter_view("factory-ready-merge-drain.yml")
    rest = 'gh api --method PUT "repos/${GITHUB_REPOSITORY}/pulls/${pr}/merge"'
    assert rest in drain
    assert '-f sha="$expected_head" -f merge_method=merge' in drain
    assert drain.index("--merge --match-head-commit") < drain.index(rest)
    assert "-merge-refused-v1:pr-${pr}:head-${expected_head}" in drain
    assert 'grep -qF "$marker"' in drain


def test_a_workflow_permission_refusal_retries_once_with_the_operator_token() -> None:
    """#5848 changed ci.yml; the Actions token cannot hold the workflows permission."""
    drain = adopter_view("factory-ready-merge-drain.yml")
    assert "WORKFLOW_MERGE_TOKEN: ${{ secrets.PR_REBASE_TOKEN }}" in drain
    retry = drain.index('GH_TOKEN="$WORKFLOW_MERGE_TOKEN" gh api --method PUT')
    assert drain.index("without `workflows` permission") < retry
    assert drain.index('-f sha="$expected_head" -f merge_method=merge', retry) > retry
    assert retry < drain.index("-merge-refused-v1:pr-${pr}")
