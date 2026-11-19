"""Regression coverage for scheduled factory issue-state reconciliation."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
WORKFLOW = Path(".github/workflows/factory-issue-pr-state-reconciler.yml")
POLICY_SCRIPT = SCRIPTS / "factory_issue_pr_state_policy.py"


def _load_module(name: str, path: Path) -> ModuleType:
    """Load a factory script as a module from its on-disk path."""
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _policy() -> ModuleType:
    """Load issue-state policy after work-policy is importable."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    _load_module("factory_work_policy", SCRIPTS / "factory_work_policy.py")
    return _load_module("factory_issue_pr_state_policy", POLICY_SCRIPT)


def _reconcile_issue_body(workflow: str) -> str:
    """Return the bash reconcile_issue function from the workflow file."""
    start = workflow.index("reconcile_issue() {")
    end = workflow.index("if [[ \"$EVENT_NAME\" == 'pull_request_target' ]]", start)
    return workflow[start:end]


def _replace_issue_state_body(workflow: str) -> str:
    """Return the bash replace_issue_state writer from the workflow file."""
    start = workflow.index("replace_issue_state() {")
    end = workflow.index("reconcile_issue() {", start)
    return workflow[start:end]


def _iso_timestamp(epoch: int) -> str:
    """Return a GitHub-style UTC timestamp for an epoch."""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue_payload(labels: list[str], updated_at: str | None) -> dict[str, object]:
    """Build the decoded issue shape consumed by the lease policy."""
    return {
        "labels": [{"name": label} for label in labels],
        "updated_at": updated_at,
    }


def test_factory_issue_state_reconciler_has_periodic_sweep() -> None:
    """Old orphaned issue labels must be repaired without a new PR event."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "  schedule:" in workflow
    assert "    - cron: '31 * * * *'" in workflow


def test_hourly_sweep_skips_history_when_no_stale_stages() -> None:
    """Clean factory backlog must not trigger 5,000-PR history pagination."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    schedule = workflow.index("factory_issues=\"$(gh issue list")
    no_work_exit = workflow.index('if [[ -z "$factory_issues" ]]', schedule)
    full_history = workflow.index('SWEEP_PULLS="$(gh api --paginate', schedule)

    assert "31 * * * *" in workflow
    assert "--json number,labels" in workflow[schedule:full_history]
    assert 'factory:(building|review|changes-requested|ci|ready)' in workflow[schedule:full_history]
    assert schedule < no_work_exit < full_history
    assert "exit 0" in workflow[no_work_exit:full_history]


def test_pr_history_uses_supported_pagination_and_compacts_before_caching() -> None:
    """gh rejects --slurp combined with --jq; project per page, then reassemble."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    script = workflow[workflow.index("Reconcile linked issue from trusted canonical PR history"):]
    assert not re.search(r"--paginate\s+--slurp\s*\\\s*--jq", script)
    projection = "--jq '[.[] | {number, head: {ref: .head.ref}, state, merged_at}]'"
    assert script.count(projection) == 2
    assert script.count(" | jq -cs '.'") == 2
    assert "jq -r '.[][] | [.number, .head.ref, .state, .merged_at] | @tsv'" in script


def test_automated_issue_sweep_defers_when_installation_quota_is_low() -> None:
    """A low REST budget must not start either expensive scheduled scan."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    guard = workflow.index("Guard recovery sweeps on the Actions installation REST budget")
    enroll = workflow.index("Enroll repository PRs missing factory labels")
    history = workflow.index("Reconcile linked issue from trusted canonical PR history")
    assert guard < enroll < history
    assert "gh api rate_limit" in workflow[guard:enroll]
    assert "remaining < 1200" in workflow[guard:enroll]
    assert "allow=false" in workflow[guard:enroll]
    assert "if: steps.api_budget.outputs.allow != 'false'" in workflow[enroll:history]
    assert "if: steps.api_budget.outputs.allow != 'false'" in workflow[history:]


def test_ralph_status_blocked_issue_is_not_rewritten() -> None:
    """A ralph-status:blocked issue must not get factory labels rewritten."""
    policy = _policy()
    work_policy = sys.modules["factory_work_policy"]

    assert policy.issue_labels_block_reconciliation(
        ["factory", "factory:unowned", "factory:blocked", "ralph-status:blocked"]
    )
    for label in work_policy.BLOCKED_LABELS:
        assert policy.issue_labels_block_reconciliation([label]), label
    assert not policy.issue_labels_block_reconciliation(
        ["factory", "factory:unowned", "ralph-status:pending"]
    )


def test_blocked_labels_cli_reports_skip_for_ralph_status_blocked() -> None:
    """The reconciler CLI must report blocked so bash returns before a rewrite."""
    blocked = subprocess.run(
        [
            sys.executable,
            str(POLICY_SCRIPT),
            "--blocked-by-labels-json",
            json.dumps(["factory", "factory:unowned", "ralph-status:blocked"]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert blocked.stdout.strip() == "blocked"

    allowed = subprocess.run(
        [
            sys.executable,
            str(POLICY_SCRIPT),
            "--blocked-by-labels-json",
            json.dumps(["factory", "factory:unowned", "ralph-status:pending"]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert allowed.stdout.strip() == "ok"


def test_reconcile_issue_skips_blocked_labels_before_replace() -> None:
    """Sweep and PR paths skip inside reconcile_issue, before label replacement.

    The workflow stays enabled; only issues matching claim-policy BLOCKED_LABELS
    are left alone.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    body = _reconcile_issue_body(workflow)

    assert "if: false" not in workflow
    assert "    - cron: '31 * * * *'" in workflow
    skip_at = body.index("--blocked-by-labels-json")
    replace_at = body.index('replace_issue_state "$issue" "$stage"')
    assert skip_at < replace_at
    assert "skipped (blocked labels)" in body
    assert "return 0" in body[skip_at:replace_at]
    assert '[[ "$blocked" == "blocked" ]]' in body
    policy_source = POLICY_SCRIPT.read_text(encoding="utf-8")
    assert "from factory_work_policy import (" in policy_source
    assert "BLOCKED_LABELS" in policy_source
    assert "lease_is_stale" in policy_source
    assert "set(labels) & BLOCKED_LABELS" in policy_source


def test_manual_only_body_blocks_reconciliation() -> None:
    """A standalone manual-only directive must skip factory label rewrites."""
    policy = _policy()

    assert policy.body_blocks_reconciliation(
        "<!-- factory-execution:manual-only -->\nHold this factory PR.\n"
    )
    assert not policy.body_blocks_reconciliation("Ordinary autonomous factory work.\n")
    assert not policy.body_blocks_reconciliation(
        "To opt out, use `<!-- factory-execution:manual-only -->`."
    )
    assert policy.body_blocks_reconciliation("Hold this factory PR.\nmanual-only\n")
    assert not policy.body_blocks_reconciliation("Discussing a manual-only hold in prose.\n")


def test_blocked_by_body_cli_reports_skip_for_manual_only(tmp_path: Path) -> None:
    """The reconciler CLI must report blocked so bash returns before a rewrite."""
    hold = tmp_path / "hold.md"
    hold.write_text(
        "<!-- factory-execution:manual-only -->\nHold this factory PR.\n",
        encoding="utf-8",
    )
    blocked = subprocess.run(
        [
            sys.executable,
            str(POLICY_SCRIPT),
            "--blocked-by-body-file",
            str(hold),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert blocked.stdout.strip() == "blocked"

    ordinary = tmp_path / "ordinary.md"
    ordinary.write_text("Ordinary autonomous factory work.\n", encoding="utf-8")
    allowed = subprocess.run(
        [
            sys.executable,
            str(POLICY_SCRIPT),
            "--blocked-by-body-file",
            str(ordinary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert allowed.stdout.strip() == "ok"


def test_reconcile_issue_skips_manual_only_before_replace() -> None:
    """Open PRs with the marker must not be rewritten to factory:unowned.

    ``gh issue list --label factory`` includes pull requests, so the body
    skip has to live inside reconcile_issue with the BLOCKED_LABELS skip.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    body = _reconcile_issue_body(workflow)

    skip_at = body.index("--blocked-by-body-file")
    replace_at = body.index('replace_issue_state "$issue" "$stage"')
    assert skip_at < replace_at
    assert "skipped (manual-only)" in body
    assert "return 0" in body[skip_at:replace_at]
    assert '[[ "$blocked" == "blocked" ]]' in body[skip_at:replace_at]


def test_fresh_local_lease_is_preserved_over_historical_pr_history() -> None:
    """A live local claim must survive a sweep that sees only closed PRs.

    #4457/#4345: applying factory:local + factory:building + ralph-status,
    then a historical-PR sweep replaces it with factory:unowned. The issue is
    still being edited, so its updated_at proves the lease is fresh.
    """
    policy = _policy()
    now = 1_800_000_000

    assert policy.lease_action_for_issue(
        _issue_payload(
            ["factory", "factory:local", "factory:building", "ralph-status:in-progress"],
            _iso_timestamp(now - 5),
        ),
        now_epoch=now,
    ) == policy.LEASE_ACTION_HOLD_LOCAL


def test_local_lease_with_unknown_activity_is_preserved() -> None:
    """Missing freshness evidence is unknown, not stale, so the lease holds."""
    policy = _policy()
    now = 1_800_000_000

    assert policy.lease_action_for_issue(
        _issue_payload(["factory", "factory:local"], None),
        now_epoch=now,
    ) == policy.LEASE_ACTION_HOLD_LOCAL


def test_provably_stale_local_lease_falls_through_to_history() -> None:
    """The shared local lease TTL still permits an authoritative release."""
    policy = _policy()
    now = 1_800_000_000
    labels = ["factory", "factory:local", "factory:building"]
    ttl = sys.modules["factory_work_policy"].LOCAL_LEASE_TTL_SECONDS

    assert policy.lease_action_for_issue(
        _issue_payload(labels, _iso_timestamp(now - ttl - 1)),
        now_epoch=now,
    ) == policy.LEASE_ACTION_RECONCILE
    # Exactly at the TTL is not yet stale.
    assert policy.lease_action_for_issue(
        _issue_payload(labels, _iso_timestamp(now - ttl)),
        now_epoch=now,
    ) == policy.LEASE_ACTION_HOLD_LOCAL


def test_local_lease_with_redundant_unowned_still_holds() -> None:
    """A contradictory unowned marker must not turn an active local lease off.

    A no-persisted-change handoff can leave factory:local and factory:unowned
    together. Visibility enrollment already treats the local lease as active;
    the history sweep must do the same instead of reading unowned as released.
    """
    policy = _policy()
    now = 1_800_000_000

    assert policy.lease_action_for_issue(
        _issue_payload(
            ["factory", "factory:local", "factory:unowned", "factory:building"],
            _iso_timestamp(now - 5),
        ),
        now_epoch=now,
    ) == policy.LEASE_ACTION_HOLD_LOCAL


def test_numbered_lease_is_never_stolen_without_active_run_evidence() -> None:
    """The issue sweep has no fixed-worker evidence, so numbered owners hold."""
    policy = _policy()
    now = 1_800_000_000

    assert policy.lease_action_for_issue(
        _issue_payload(["factory", "factory:7", "factory:building"], _iso_timestamp(now - 10_000)),
        now_epoch=now,
    ) == policy.LEASE_ACTION_HOLD_NUMBERED


def test_numbered_lease_release_requires_authoritative_worker_evidence() -> None:
    """The helper applies the shared fixed-lease TTL only with real evidence."""
    policy = _policy()
    now = 1_800_000_000
    labels = ["factory", "factory:7", "factory:building"]
    ttl = _load_module("factory_work_policy", SCRIPTS / "factory_work_policy.py").FIXED_LEASE_TTL_SECONDS

    # Before expiry, no history reconciliation may steal a numbered lease.
    assert policy.lease_reconciliation_action(
        labels,
        latest_activity_epoch=now - ttl + 1,
        now_epoch=now,
        active_fixed_workers=set(),
        has_unresolved_active_runs=False,
    ) == policy.LEASE_ACTION_HOLD_NUMBERED
    assert policy.lease_reconciliation_action(
        labels,
        latest_activity_epoch=now - ttl - 1,
        now_epoch=now,
        active_fixed_workers=set(),
        has_unresolved_active_runs=False,
    ) == policy.LEASE_ACTION_RECONCILE
    assert policy.lease_reconciliation_action(
        labels,
        latest_activity_epoch=now - 10_000,
        now_epoch=now,
        active_fixed_workers={7},
        has_unresolved_active_runs=False,
    ) == policy.LEASE_ACTION_HOLD_NUMBERED


def test_conflicting_owners_are_preserved_for_operator_review() -> None:
    """Never guess which of two active owners survives a sweep."""
    policy = _policy()

    assert policy.lease_action_for_issue(
        _issue_payload(["factory", "factory:local", "factory:7"], _iso_timestamp(1_799_999_000)),
        now_epoch=1_800_000_000,
    ) == policy.LEASE_ACTION_HOLD_AMBIGUOUS


def test_released_owner_does_not_block_history_reconciliation() -> None:
    """factory:unowned is a released owner, not a live lease."""
    policy = _policy()

    assert policy.lease_action_for_issue(
        _issue_payload(["factory", "factory:unowned", "factory:review"], None),
        now_epoch=1_800_000_000,
    ) == policy.LEASE_ACTION_RECONCILE


def test_lease_action_cli_reports_hold_for_active_local_lease(tmp_path: Path) -> None:
    """The workflow's real CLI call returns a machine-readable hold."""
    policy = _policy()
    now = 1_800_000_000
    issue_file = tmp_path / "issue.json"
    issue_file.write_text(
        json.dumps(
            _issue_payload(
                ["factory", "factory:local", "factory:building"],
                _iso_timestamp(now - 5),
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(POLICY_SCRIPT),
            "--lease-action-issue-file",
            str(issue_file),
            "--now-epoch",
            str(now),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == policy.LEASE_ACTION_HOLD_LOCAL


def test_claim_after_history_walk_is_caught_at_writer_boundary(tmp_path: Path) -> None:
    """A claim landing after the history walk must stop the writer.

    The pre-repair guard read the issue at T1 and the writer re-read labels at
    T2, so a claim in that window was visible to the write yet discarded. The
    writer now reads once at the boundary, applies the shared lease policy to
    that payload, and builds the replacement labels from the same payload.
    """
    policy = _policy()
    now = 1_800_000_000
    issue_file = tmp_path / "boundary-issue.json"
    issue_file.write_text(
        json.dumps(
            _issue_payload(
                ["factory", "factory:local", "factory:building", "ralph-status:in-progress"],
                _iso_timestamp(now - 1),
            )
        ),
        encoding="utf-8",
    )

    decision = subprocess.run(
        [
            sys.executable,
            str(POLICY_SCRIPT),
            "--lease-action-issue-file",
            str(issue_file),
            "--now-epoch",
            str(now),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert decision.stdout.strip() == policy.LEASE_ACTION_HOLD_LOCAL

    workflow = WORKFLOW.read_text(encoding="utf-8")
    body = _replace_issue_state_body(workflow)
    read_at = body.index('gh api "repos/${GITHUB_REPOSITORY}/issues/${issue}"')
    policy_at = body.index("--lease-action-issue-file")
    labels_at = body.index('labels="$(jq')
    put_at = body.index('--method PUT')
    assert read_at < policy_at < labels_at < put_at
    assert "return 0" in body[policy_at:put_at]
    # Both the policy decision and the target labels consume the boundary read.
    assert '"$issue_file"' in body[read_at:labels_at]
    assert '"$issue_file"' in body[labels_at:put_at]
    # No second labels fetch can discard an owner the policy just accepted.
    assert "/labels?per_page=100" not in body


def test_reconcile_issue_has_no_separate_pre_write_lease_read() -> None:
    """The history walk must not duplicate the writer's boundary ownership read."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    body = _reconcile_issue_body(workflow)

    assert "--lease-action-issue-file" not in body
    assert "lease_file" not in body
    assert body.count('replace_issue_state "$issue" "$stage"') == 1
    assert workflow.count("--lease-action-issue-file") == 1


def test_trusted_review_comment_events_route_to_visibility_without_issue_sweep() -> None:
    """Human review comments must reach the label handler without heavy sweeps."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "issue_comment:\n    types: [created]" in workflow
    assert "github.event.issue.pull_request" in workflow
    assert "contains(github.event.comment.body, 'comic-pile-factory-semantic-review-v1:')" in workflow
    assert "context.eventName === 'issue_comment'" in workflow
    assert '[[ "$EVENT_NAME" == ' in workflow
    assert "== 'issue_comment' ||" in workflow
    # Bot-authored comments cannot enqueue an Actions workflow via GITHUB_TOKEN.
    # The same reviewed head is labeled directly in the trusted controller.
    controller = (SCRIPTS / "factory-review-controller.py").read_text(encoding="utf-8")
    assert "set_review_approval_visibility(pr_number, reviewed_head)" in controller
    assert controller.index('pr_stage="factory:ready",') < controller.index(
        "set_review_approval_visibility(pr_number, reviewed_head)"
    )
