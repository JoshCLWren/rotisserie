#!/usr/bin/env python3
"""Deterministic GitHub assignment and lease reconciliation for the adopter."""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
sys.path.insert(0, os.path.dirname(__file__))
# OmniRoute enable gate: FACTORY_OMNIROUTE_ENABLED (default off).
from factory_capacity_policy import (
    DEFAULT_MULTI_PROVIDER_ENTRY_CAP,
    DEFAULT_OMNIROUTE_FREE_ENTRY_CAP,
    omniroute_enabled,
    remaining_omniroute_free_entry_slots,
)
from factory_work_policy import (BLOCKED_LABELS, agent_hold, FACTORY_NO_DIFF_RETRY_LIMIT, FACTORY_NO_DIFF_RETRY_RESET_SECONDS, FACTORY_PR_WIP_LIMIT, FACTORY_REVIEW_BACKLOG_LIMIT, FIXED_LEASE_TTL_SECONDS, LOCAL_LEASE_TTL_SECONDS, FIXED_OWNER_RE, NoDiffAttempt, NON_EXECUTABLE_ISSUES, OWNER_RE, REQUIRED_CHECK_FAILURE_STATES, STAGE_LABELS, STAGE_PRECEDENCE, Candidate, build_candidates, comment_is_trusted, completion_action_for, eligibility, explain_issue_report, provenance_lane, env_positive_int, factory_pr_wip_count, factory_review_backlog_count, is_manual_only, item_is_unowned, issue_bypasses_wip_limit, issue_explicitly_closed_by_pr, labels_of, lease_is_stale, linked_issue_from_branch, linked_issue_from_pr, order_candidates_for_worker, owner_of, parse_no_diff_attempts_from_comments, plan_distinct_assignments, pr_suppresses_issue_candidate, stage_of)
from factory_jev_shadow import apply_completion_ranking, apply_jev_ranking
from factory_explain_report import render_explain_report
from stale_pr_decay import StalePRGuard, list_factory_prs
from factory_review_policy import recorded_pr_contributors, trusted_comment_bodies
from factory_quota_pause import describe as describe_quota_pause, is_paused as quota_pause_for
from factory_source_budget import budget_block_reason, worker_sources
from factory_attempt_history import read_comment_history
from factory_discovery import canonical_target, discover, update_labels
from factory_state_ledger import acquire_lease, forced_refusal, release_lease, worker_claim, external_wip_count
import factory_active_workers
import factory_crash
import factory_attempt_history_store
import factory_lease_owners
import factory_state_ledger
from factory_lease_owners import ActiveWorkerResult, github_timestamp_epoch
from factory_state_ledger import enabled as ledger_enabled
from factory_adopter import MARKER_PREFIX, repository
import factory_assign_trace as trace
import factory_deps
import factory_dispatch_holds
import factory_ready
import factory_issue_intake_guard
from factory_pr_merge_state import fill_merge_state
REPO = repository()
GH_TIMEOUT_SECONDS = env_positive_int("FACTORY_GH_TIMEOUT_SECONDS", 120)
ASSIGNMENT_WRITER_WORKFLOW_PATH = ".github/workflows/fixed-model-factory-dispatch.yml"
ROTISSERIE_CONTROL_PATH = Path(".github/factory-rotisserie-control.json")
STRIKE_RESET_RE = re.compile(
    rf"{MARKER_PREFIX}-factory-strike-reset-v1:issue-(?P<issue>\d+):pr-(?P<pr>\d+):"
    r"excluded-producer-(?P<worker>\d+|unknown)"
)
IMPLEMENT_CLAIM_RE = re.compile(
    rf"{MARKER_PREFIX}-factory-implement-claim-v3:issue-(?P<issue>\d+):"
    r"opencode-(?:free-model|nvidia|omniroute)-factory-(?P<worker>\d+):"
)
LEASE_ACTIVITY_PATTERNS = (
    re.compile(rf"{MARKER_PREFIX}-factory-implement-(?:claim|progress)-v3:issue-\d+:[^:>]+:(\d{{10}})"),
    re.compile(rf"{MARKER_PREFIX}-factory-fix-(?:claim|progress)-v3:[^:>]+:[^:>]+:(\d{{10}})"),
    re.compile(rf"{MARKER_PREFIX}-factory-review-claim-v2:[^:>]+:[^:>]+:(\d{{10}})"),
    re.compile(rf"{MARKER_PREFIX}-factory-controller-claim-v1:(?:issue|pr)-\d+:\d+:(\d{{10}})"),
)
# Bound on exact-head contributor reads per dispatch pass. Resolving provenance
# costs one paginated comment read per review-stage candidate, so the pass is
# capped rather than scaled with the whole review backlog.
REVIEW_CONTRIBUTOR_LOOKUP_LIMIT = env_positive_int(
    "FACTORY_REVIEW_CONTRIBUTOR_LOOKUP_LIMIT", 40
)
__all__ = ["linked_issue_from_branch", "plan_distinct_assignments", "signal_completion_mode", "signal_roster_mode"]


def run_gh(args: list[str], *, input_json: object | None = None, check: bool = True) -> str:
    """Run a bounded GitHub CLI command and return stdout."""
    command, stdin = ['gh', *args], None if input_json is None else json.dumps(input_json)
    try:
        proc = factory_crash.gh_with_retry(command, lambda: subprocess.run(command, input=stdin, text=True, capture_output=True, check=False, timeout=GH_TIMEOUT_SECONDS), stdin=stdin)
    except subprocess.TimeoutExpired as exc:
        raise factory_crash.GitHubCallError(f"{' '.join(command)} timed out") from exc
    if check and proc.returncode:
        raise factory_crash.GitHubCallError(f"{' '.join(command)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def gh_json(args: list[str], *, input_json: object | None = None) -> object | None:
    """Run GitHub CLI and decode JSON stdout."""
    output = run_gh(args, input_json=input_json)
    return json.loads(output) if output.strip() else None


def list_issues() -> list[dict[str, Any]]:
    """Read one run-local discovery inventory; authorization stays live."""
    hint = os.environ.get('FACTORY_DISCOVERY_TARGET', '')
    if re.fullmatch(r'(issue|pr):[1-9][0-9]*', hint):
        kind, raw_number = hint.split(':')
        number = int(raw_number)
        rows: list[dict[str, Any]] = []
        if kind == 'pr':
            pr = list_prs()[0]
            number = linked_issue_from_pr(pr)
        if number is not None:
            rows.append(canonical_target('issue', target_json(number)))
        return rows
    return discover(REPO, 'issue', lambda: cast(list[dict[str, Any]], gh_json([
        'issue', 'list', '--repo', REPO, '--state', 'open', '--limit', '1000',
        '--json', 'number,title,body,labels,createdAt,updatedAt',
    ])))


def list_prs() -> list[dict[str, Any]]:
    """Share PR discovery only within this trusted job, never between runs."""
    hint = os.environ.get('FACTORY_DISCOVERY_TARGET', '')
    if re.fullmatch(r'pr:[1-9][0-9]*', hint):
        number = int(hint.split(':')[1])
        return discover(REPO, 'pr', lambda: [canonical_target('pr', live_pr_state(number))])
    return discover(REPO, 'pr', lambda: fill_merge_state(cast(list[dict[str, Any]], gh_json([
        'pr', 'list', '--repo', REPO, '--state', 'open', '--limit', '500',
        '--json', 'number,title,body,labels,headRefName,headRefOid,createdAt,updatedAt,isDraft,mergeable,mergeStateStatus,isCrossRepository',
    ])), live_pr_state))


def rotisserie_issue_intake(issues: list[dict[str, Any]], prs: list[dict[str, Any]], *, operator_force: bool = False) -> set[int] | None:
    """Return authorized issue numbers, or None while the lane is legacy.

    A deliberate operator-forced worker can bypass *soft* WIP/review-backlog
    pressure in the portable Rotisserie decision, but the same snapshot still
    enforces human boundaries, incomplete dependencies and existing changes.
    This does not bypass live GitHub or store claim checks.
    """
    try:
        control = json.loads(ROTISSERIE_CONTROL_PATH.read_text(encoding="utf-8"))
        if isinstance(control, dict) and control.get("stage") == "legacy":
            return None
        from factory_rotisserie_adopter import issue_intake

        revision = os.environ.get("GITHUB_SHA", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip().lower()
        return issue_intake(
            control,
            issues,
            prs,
            revision=revision,
            now=int(time.time()),
            command=[os.environ.get("FACTORY_ROTISSERIE_COMMAND", "rotisserie")],
            operator_force=operator_force,
        )
    except (KeyError, OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"[factory-controller] Rotisserie issue intake unavailable; holding fresh issues: {error}", file=sys.stderr)
        return set()


def target_json(number: int) -> dict[str, Any]:
    """Fetch one issue-compatible GitHub target payload."""
    return cast(dict[str, Any], gh_json(['api', f'repos/{REPO}/issues/{number}']))


def live_pr_state(number: int) -> dict[str, Any]:
    """Read the authoritative PR head, merge state, and labels at lease time."""
    return cast(dict[str, Any], gh_json(['api', f'repos/{REPO}/pulls/{number}']))


def last_github_update_epoch(item: dict[str, Any], *, now_epoch: int) -> int | None:
    """Return GitHub's latest target update, never the creation age.

    Local integrations do not always emit trusted lease markers. GitHub still
    advances updatedAt/updated_at when a claim, comment, or progress edit is
    persisted, so old claims have a bounded inactivity period without making
    missing or untrusted markers into permanent leases.
    """
    raw = item.get("updatedAt") or item.get("updated_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    epoch = int(timestamp.timestamp())
    return epoch if 0 < epoch <= now_epoch else None


def refresh_discovery_after_label_change(number: int, target: dict[str, Any]) -> None:
    """Update only local scheduling memory; never persist GitHub projections."""
    try:
        update_labels(REPO, 'pr' if 'pull_request' in target else 'issue',
                      number, target.get('labels') or [])
    except (OSError, ValueError):
        print('[factory-controller] local discovery refresh unavailable; live claim checks remain required', file=sys.stderr)


def replace_factory_labels(
    number: int,
    owner: str,
    stage: str | None = None,
    *,
    expected_owner: str | None = None,
    inactive_before_epoch: int | None = None,
    observed_at_epoch: int | None = None,
    related_owner_target: int | None = None,
) -> bool:
    """Atomically reconcile one target to exactly one owner and workflow stage.

    The repository policy requires a single full label-set replacement rather
    than sequential add/remove mutations, which would expose contradictory
    intermediate owner states. A post-write claim verification is performed by
    ``assign_candidate``.
    """
    if (
        FIXED_OWNER_RE.fullmatch(owner)
        and os.environ.get('GITHUB_ACTIONS', '').strip().lower() == 'true'
        and not dispatcher_identity_verified()
    ):
        raise RuntimeError('dispatcher authorization failed: mutation blocked for non-dispatcher workflow')
    target = target_json(number)
    if expected_owner is not None:
        # Stale cleanup must not overwrite a new local claim or fresh progress
        # that arrived between the queue snapshot and the label mutation.
        if owner_of(labels_of(target)) != expected_owner:
            return False
        if str(target.get("state") or "OPEN").upper() != "OPEN":
            return False
        # Explicit exit cleanup checks ownership but does not require staleness.
        # Stale reclamation remains subject to the inactivity watermark.
        if inactive_before_epoch is not None:
            checked_at = int(time.time()) if observed_at_epoch is None else observed_at_epoch
            updated = last_github_update_epoch(target, now_epoch=checked_at)
            if updated is None or updated > inactive_before_epoch:
                return False
        if related_owner_target is not None and inactive_before_epoch is not None:
            # An issue and its canonical PR are one active local repair.
            # Progress on either side must protect the paired owner lease.
            related = target_json(related_owner_target)
            if owner_of(labels_of(related)) == expected_owner:
                related_update = last_github_update_epoch(
                    related, now_epoch=checked_at
                )
                if related_update is None or related_update > inactive_before_epoch:
                    return False
    # Shared write choke point: never add, repair, or re-apply factory labels
    # on a manual-only hold. Leftover factory* labels stay for humans.
    if is_manual_only(target.get("body")) or agent_hold(labels_of(target)):
        print(
            f"[factory-controller] skip factory label write on #{number}: manual-only or outside agent hold",
            file=sys.stderr,
        )
        return False
    current = [label['name'] for label in target.get('labels', [])]
    # Normalize state based on global policy
    from factory_work_policy import normalize_target_state
    norm_owner, norm_stage = normalize_target_state(set(current), owner)
    # Use requested stage if provided, otherwise use normalized stage.
    # Normalized owner/stage enforce invariants (e.g. done removes transient
    # labels; unowned removes building; blocked unowned gets factory:blocked).
    final_stage = stage if stage is not None else norm_stage
    final_owner = norm_owner

    # Remove all existing owner labels (including factory:unowned) and stage labels
    # factory:available is only ever re-derived by the label doctor (#191).
    labels = [label for label in current if not OWNER_RE.fullmatch(label) and label not in STAGE_LABELS and label not in ('factory', 'factory:available')]
    labels.extend(['factory', final_owner])
    if final_stage:
        labels.append(final_stage)
    # Enforce ralph-status invariant: in-progress without real owner -> pending
    if 'ralph-status:in-progress' in current and final_owner == 'factory:unowned':
        labels = [label for label in labels if label != 'ralph-status:in-progress']
        labels.append('ralph-status:pending')
    run_gh(['api', '--method', 'PUT', f'repos/{REPO}/issues/{number}/labels', '--input', '-'], input_json={'labels': sorted(set(labels))})
    target['labels'] = [{'name': name} for name in sorted(set(labels))]
    refresh_discovery_after_label_change(number, target)
    return True


def issue_has_open_blocker(number: int, *, live: bool = False) -> bool:
    """Return whether an issue has an open dependency blocker.

    Ranking reads can use a bounded store snapshot. Actual issue assignment
    explicitly rechecks the authoritative GitHub dependency list immediately
    before claiming a transactional lease, bypassing the discovery cache.
    """
    args = ['api']
    if live:
        args += ['--header', 'Cache-Control: no-cache']
    args.append(f'repos/{REPO}/issues/{number}/dependencies/blocked_by?per_page=100')
    try:
        blockers = cast(list[dict[str, Any]], gh_json(args) or [])
    except RuntimeError:
        return True
    return any(str(item.get('state') or '').lower() == 'open' for item in blockers)


# Required CI is the scheduler's signal, not work for a model to watch.
# A completed failure takes precedence only after all required checks settle.
REQUIRED_CHECK_PASSING_STATES = frozenset({'SUCCESS', 'NEUTRAL', 'SKIPPED'})


def required_checks_outcome(pr_number: int) -> str:
    """Classify required checks on the current PR head: waiting, failed, passed.

    Treat missing, unparseable and unknown evidence as waiting. Never turn a
    partial failure into a model assignment while another required check runs.
    The trusted CI reconciler handles green/failed transitions without models.
    """
    try:
        output = run_gh(
            ['pr', 'checks', str(pr_number), '--repo', REPO, '--required', '--json', 'state'],
            check=False,
        )
        checks = json.loads(output) if output.strip() else None
    except (RuntimeError, json.JSONDecodeError):
        return 'waiting'
    if not isinstance(checks, list) or not checks:
        return 'waiting'
    states: list[str] = []
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('state'), str):
            return 'waiting'
        states.append(check['state'].upper())
    terminal = REQUIRED_CHECK_FAILURE_STATES | REQUIRED_CHECK_PASSING_STATES
    if any(state not in terminal for state in states):
        return 'waiting'
    if any(state in REQUIRED_CHECK_FAILURE_STATES for state in states):
        return 'failed'
    return 'passed'


def required_checks_failed(pr_number: int) -> bool:
    """Return True only for terminal required-check failure on the current head."""
    return required_checks_outcome(pr_number) == 'failed'


def conflicted_head_without_published_ci(pr_number: int) -> bool:
    """Verify a conflict head has no CI still running, rather than hiding running checks.

    GitHub reports no required checks on a conflicted PR, and gh can emit a
    non-JSON 'no checks reported' error, so gh pr checks alone cannot tell
    settled CI from unknown evidence. Only the authoritative check-runs
    endpoint for the current head can make this narrow exception safe. CI that
    already ran (a published CI Summary) settles nothing a merge conflict
    waits on, so only a still-running check holds the repair (#182). This
    does not authorize merging.
    """
    try:
        live = live_pr_state(pr_number)
        head = str((live.get('head') or {}).get('sha') or '')
        if (
            str(live.get('state') or '').lower() != 'open'
            or not re.fullmatch(r'[0-9a-f]{40}', head)
            or not (
                live.get('mergeable') is False
                or str(live.get('mergeable_state') or '').upper() == 'DIRTY'
            )
        ):
            return False
        response = gh_json([
            'api', f'repos/{REPO}/commits/{head}/check-runs?per_page=100',
        ])
    except (RuntimeError, ValueError, TypeError, KeyError):
        return False
    if not isinstance(response, dict):
        return False
    runs = response.get('check_runs')
    total = response.get('total_count')
    if (
        not isinstance(runs, list)
        or not isinstance(total, int)
        or total != len(runs)
        or any(not isinstance(run, dict) for run in runs)
    ):
        # Pagination, malformed data or an unavailable read is not evidence
        # that required CI is absent.
        return False
    if any(str(run.get('status') or '').lower() != 'completed' for run in runs):
        # A still-running head check must finish first.
        return False
    return True


def candidate_is_live_executable(candidate: Candidate) -> bool:
    """Return whether a ranked candidate can use a model *now*.

    CI-in-progress suppresses every PR stage, including repair and conflict
    work. A verified merge conflict *without published required CI* must be
    repairable before CI can run. Review, lease and merge gates stay intact.
    """
    if candidate.kind == 'issue':
        return not issue_has_open_blocker(candidate.number)
    target = target_json(candidate.number)
    labels = {label['name'] for label in target.get('labels', [])}
    if 'factory:ready' in labels:
        return False
    outcome = required_checks_outcome(candidate.number)
    if outcome == 'waiting':
        if not candidate.conflicted or 'factory:ci' in labels:
            return False
        return conflicted_head_without_published_ci(candidate.number)
    if 'factory:ci' in labels:
        return outcome == 'failed'
    return True


def issue_excludes_worker_on_strike_retry(number: int, worker: str) -> bool:
    """Keep the failed producer out of the first clean retry implementation."""
    try:
        pages = gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{REPO}/issues/{number}/comments?per_page=100",
            ]
        )
    except RuntimeError:
        # Do not stall unrelated issue intake on a transient comment read failure.
        return False
    reset_worker: str | None = None
    reset_seen = False
    for comment in flatten_pages(pages):
        if not comment_is_trusted(comment):
            continue
        body = str(comment.get("body") or "")
        reset = STRIKE_RESET_RE.search(body)
        if reset and int(reset.group("issue")) == number:
            reset_worker = reset.group("worker")
            reset_seen = True
            continue
        if reset_seen:
            claim = IMPLEMENT_CLAIM_RE.search(body)
            if claim and int(claim.group("issue")) == number:
                # Once a clean retry actually begins, the one-shot exclusion is spent.
                reset_seen = False
                reset_worker = None
                continue
    return reset_seen and reset_worker == worker


def target_still_unowned(number: int) -> bool:
    """Return whether a target remains safely claimable."""
    labels = {label['name'] for label in target_json(number).get('labels', [])}
    return item_is_unowned(labels) and not bool(labels & BLOCKED_LABELS)


def target_owned_by(number: int, owner: str) -> bool:
    """Return whether exactly one expected active owner holds a target."""
    labels = {label['name'] for label in target_json(number).get('labels', [])}
    active_owners = {label for label in labels if OWNER_RE.fullmatch(label) and label != 'factory:unowned'}
    return active_owners == {owner}


def dispatcher_identity_verified() -> bool:
    """Return True only when running inside the canonical dispatcher workflow.

    Local tests and operator diagnostics remain usable outside GitHub Actions.
    In Actions, authorization is tied to the repository-qualified workflow file
    identity rather than the non-unique workflow display name.
    """
    if os.environ.get('GITHUB_ACTIONS', '').strip().lower() != 'true':
        return True
    repository = os.environ.get('GITHUB_REPOSITORY', '').strip()
    workflow_ref = os.environ.get('GITHUB_WORKFLOW_REF', '').strip()
    workflow_file = workflow_ref.partition('@')[0]
    return bool(repository) and workflow_file == (
        f"{repository}/{ASSIGNMENT_WRITER_WORKFLOW_PATH}"
    )


def record_controller_lease_activity(
    number: int,
    worker: str,
    kind: str,
    *,
    action: str | None = None,
    source: str | None = None,
    head: str | None = None,
) -> None:
    """Persist a trusted lease timestamp and optional decision provenance."""
    epoch = int(time.time())
    marker = f'<!-- {MARKER_PREFIX}-factory-controller-claim-v1:{kind}-{number}:{worker}:{epoch}'
    for field, value in (('source', source), ('action', action)):
        if value is None:
            continue
        if not re.fullmatch(r'[a-z][a-z-]{0,31}', value):
            raise ValueError(f'unsafe lease provenance field: {field}')
        marker += f':{field}={value}'
    if head is not None:
        if not re.fullmatch(r'[0-9a-f]{7,40}', head):
            raise ValueError('unsafe lease head reference')
        marker += f':head={head}'
    marker += ' -->'
    run_gh(['issue', 'comment', str(number), '--repo', REPO, '--body', marker])


# Outside-agent WIP claims the current pass planned new intake against. New
# issue leases re-check it under the ledger lock (None: no plan, no check).
EXTERNAL_WIP_SEEN: int | None = None


def assign_candidate(
    candidate: Candidate,
    worker: str,
    action: str | None = None,
    expected_head: str | None = None,
    source: str | None = None,
) -> bool:
    """Claim a candidate and any linked issue for one fixed-model worker.

    Enforces the invariant: an executable issue must never simultaneously satisfy
    both 'factory:unowned' and active factory ownership. ``action`` and
    ``expected_head`` are validated against an authoritative PR read immediately
    before the lease is written, so a stale Jev decision is never executed.
    ``source`` records decision provenance (for example a Jev-ranked issue) on
    the durable lease marker.
    """
    if os.environ.get('GITHUB_ACTIONS', '').strip().lower() == 'true' and not dispatcher_identity_verified():
        return trace.skip(candidate.number, 'dispatcher workflow identity not verified')
    owner = f'factory:{worker}'
    numbers = [candidate.number]
    if candidate.kind == 'pr' and candidate.linked_issue is not None:
        try:
            issue = target_json(candidate.linked_issue)
        except RuntimeError:
            issue = None
        if issue and issue.get('state') == 'open':
            numbers.insert(0, candidate.linked_issue)
    for number in numbers:
        if not target_still_unowned(number):
            return trace.skip(candidate.number, f'#{number} is owned or blocked on GitHub')
    # The ranked snapshot may have been taken before a CI run started. Do not
    # allocate a store lease to a PR that is currently waiting on CI.
    if candidate.kind == 'pr' and candidate.stage in STAGE_LABELS:
        if not candidate_is_live_executable(candidate):
            return trace.skip(candidate.number, 'waiting on CI or ready')
    if candidate.kind == 'issue' and issue_has_open_blocker(candidate.number, live=True):
        # A fresh dependency was added after ranking; never convert an
        # advisory store cache hit into a blocked GitHub lease mutation.
        return trace.skip(candidate.number, 'open dependency blocker')
    if worker_has_active_lease(worker):
        return trace.skip(candidate.number, f'Factory {worker} already owns a GitHub target')

    # Acquire one atomic worker + issue/PR reservation before mutating GitHub.
    # GitHub live checks remain required; a store conflict never grants a claim.
    try:
        claim_token = acquire_lease(
            REPO, worker, numbers, FIXED_LEASE_TTL_SECONDS,
            external_wip_seen=EXTERNAL_WIP_SEEN if candidate.kind == 'issue' else None,
        )
    except Exception as exc:
        if type(exc).__name__ not in ('ClaimsPaused', 'StoreBlocked'):
            raise RuntimeError(f'factory store unavailable during assignment ({type(exc).__name__})') from exc
        return trace.skip(candidate.number, f'new claims paused: {exc}')
    if claim_token is None:
        return trace.skip(candidate.number, trace.store_conflict(REPO, worker, numbers))

    def release_verified_claims(claimed_numbers: list[int]) -> None:
        """Release only labels this worker can still prove it owns."""
        try:
            for claimed_number in claimed_numbers:
                try:
                    if target_owned_by(claimed_number, owner):
                        replace_factory_labels(claimed_number, 'factory:unowned')
                except Exception:
                    pass
        finally:
            # A failed GitHub write must not strand the store claim.
            try:
                release_lease(REPO, worker, claim_token, 'claim-rollback')
            except Exception as exc:
                print(f'[factory-controller] store claim rollback failed ({type(exc).__name__})', file=sys.stderr)
    claimed: list[int] = []
    try:
        # Close the race between the first check and the store reservation.
        # A newly queued CI run must release the reservation before labels,
        # comments, or model execution are written to GitHub.
        if candidate.kind == 'pr' and candidate.stage in STAGE_LABELS:
            if not candidate_is_live_executable(candidate):
                release_verified_claims(claimed)
                return False
        for number in numbers:
            # Revalidate immediately before the first lease mutation so a stale
            # idle snapshot cannot create a second lease.
            if not target_still_unowned(number):
                release_verified_claims(claimed)
                return False
            if expected_head is not None and number == candidate.number:
                try:
                    live = live_pr_state(number)
                    live_head = str((live.get('head') or {}).get('sha') or '')
                    live_labels = {
                        label['name']
                        for label in live.get('labels', [])
                        if isinstance(label, dict) and label.get('name')
                    }
                    live_conflicted = (
                        live.get('mergeable') is False
                        or str(live.get('mergeable_state') or '').upper() == 'DIRTY'
                    )
                    live_action = completion_action_for(
                        stage_of(live_labels), live_conflicted
                    )
                except (RuntimeError, KeyError, TypeError):
                    release_verified_claims(claimed)
                    return False
                if live_head != expected_head:
                    print(
                        f'[factory-controller] Jev stale exact head for PR #{number} '
                        f'(expected {expected_head[:12]}, live {live_head[:12] or "unknown"}); '
                        'discarding recommendation',
                        file=sys.stderr,
                    )
                    release_verified_claims(claimed)
                    return False
                expected_action = action
                if source == 'manual':
                    expected_action = completion_action_for(candidate.stage, candidate.conflicted)
                    if action == 'ci-repair' and not required_checks_failed(number):
                        release_verified_claims(claimed)
                        return False
                if expected_action is not None and live_action != expected_action:
                    print(
                        f'[factory-controller] Expected action {expected_action} no longer matches '
                        f'PR #{number} live state ({live_action}); discarding recommendation',
                        file=sys.stderr,
                    )
                    release_verified_claims(claimed)
                    return False
            if candidate.kind == 'issue':
                stage = 'factory:building'
                kind = 'issue'
            elif number == candidate.number:
                stage = 'factory:changes-requested' if candidate.conflicted else None
                kind = 'pr'
            else:
                stage = None
                kind = 'issue'
            replace_factory_labels(number, owner, stage)
            if not target_owned_by(number, owner):
                release_verified_claims(claimed)
                return False
            try:
                selected_pr = candidate.kind == 'pr' and number == candidate.number
                lease_kwargs: dict[str, str] = {}
                if selected_pr and action:
                    lease_kwargs = {'action': action, 'source': source or 'jev'}
                    if expected_head:
                        lease_kwargs['head'] = expected_head
                elif source:
                    lease_kwargs = {'source': source}
                record_controller_lease_activity(number, worker, kind, **lease_kwargs)
            except Exception:
                release_verified_claims([*claimed, number])
                return False
            claimed.append(number)
    except Exception:
        release_verified_claims(claimed)
        raise
    return True


def flatten_pages(pages: object | None) -> list[dict[str, Any]]:
    """Flatten paginated GitHub API responses into object rows."""
    result: list[dict[str, Any]] = []
    page_values = pages if isinstance(pages, list) else []
    for page in page_values:
        if isinstance(page, list):
            result.extend(item for item in page if isinstance(item, dict))
        elif isinstance(page, dict):
            result.append(page)
    return result


def pr_contributor_workers(number: int) -> set[str]:
    """Return controller-recorded contributors of one pull request's head lineage.

    An unreadable record resolves to an empty set on purpose: the dispatcher then
    keeps only its declared-producer knowledge, and the review controller still
    refuses a contributor's verdict and requires two distinct reviewers. Reading
    provenance must never be able to widen eligibility.
    """
    try:
        pages = gh_json([
            'api',
            '--paginate',
            '--slurp',
            f'repos/{REPO}/issues/{number}/comments?per_page=100',
        ])
    except RuntimeError as exc:
        print(
            f'[factory-controller] contributor provenance for PR #{number} unreadable; '
            f'review falls back to controller enforcement: {exc}',
            file=sys.stderr,
        )
        return set()
    return recorded_pr_contributors(trusted_comment_bodies(flatten_pages(pages)), pr=number)


def resolve_review_head_contributors(
    candidates: list[Candidate],
    prs: list[dict[str, Any]],
) -> list[Candidate]:
    """Bind exact-head contributors to the review candidates about to be ordered.

    #3059 makes every worker that authored the exact head ineligible to attest
    it. Excluding those workers during selection is what lets queue selection
    hand the head to another eligible reviewer on its own, instead of repeatedly
    dispatching a contributor whose verdict the controller must refuse. Only
    ``factory:review`` candidates need the record, and the number of comment
    reads is bounded so a large review backlog cannot multiply API traffic.
    """
    heads = {int(pr['number']): str(pr.get('headRefOid') or '') for pr in prs if 'number' in pr}
    review_numbers = [
        candidate.number
        for candidate in candidates
        if candidate.kind == 'pr' and candidate.stage == 'factory:review' and not candidate.head_contributors
    ]
    resolved: dict[int, frozenset[str]] = {}
    for number in review_numbers[:REVIEW_CONTRIBUTOR_LOOKUP_LIMIT]:
        if not re.fullmatch(r'[0-9a-f]{40}', heads.get(number, '')):
            continue
        resolved[number] = frozenset(pr_contributor_workers(number))
    if not resolved:
        return candidates
    return [
        replace(candidate, head_contributors=resolved[candidate.number])
        if candidate.kind == 'pr' and candidate.number in resolved
        else candidate
        for candidate in candidates
    ]


def latest_lease_activity_epoch(number: int) -> int | None:
    """Return the latest trusted lease activity marker for a target."""
    try:
        pages = gh_json(['api', '--paginate', '--slurp', f'repos/{REPO}/issues/{number}/comments?per_page=100'])
    except RuntimeError:
        return None
    latest: int | None = None
    for comment in flatten_pages(pages):
        if not comment_is_trusted(comment):
            continue
        body = str(comment.get('body') or '')
        for pattern in LEASE_ACTIVITY_PATTERNS:
            for match in pattern.findall(body):
                epoch = int(match)
                latest = epoch if latest is None else max(latest, epoch)
    return latest


def recent_factory_comments(
    now_epoch: int, *, target_pr: int | None = None,
) -> list[dict[str, Any]]:
    """Read retry history; a targeted PR only needs its own comments."""
    if target_pr is not None and target_pr <= 0:
        raise ValueError("target_pr must be a positive PR number")
    def fetch_pages(since_epoch: int) -> tuple[list[dict[str, object]], int]:
        # Round *down* to share the same store GET key across near-simultaneous
        # dispatchers. The extra comments are a safe superset; retry-window
        # filtering still uses the original now_epoch.
        since = datetime.fromtimestamp(since_epoch - since_epoch % 60, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        path = f'issues/{target_pr}/comments' if target_pr is not None else 'issues/comments'
        pages = gh_json(['api', '--paginate', '--slurp', f'repos/{REPO}/{path}?since={since}&per_page=100'])
        if (
            not isinstance(pages, list) or not pages
            or not all(isinstance(page, list) for page in pages)
            or not all(isinstance(comment, dict) for page in pages for comment in page)
        ):
            raise RuntimeError('retry history pagination returned an invalid response')
        return flatten_pages(pages), len(pages)

    cache = os.environ.get('FACTORY_ATTEMPT_HISTORY_CACHE', '')
    shared = factory_attempt_history_store.for_repo(REPO, reset_seconds=FACTORY_NO_DIFF_RETRY_RESET_SECONDS) if cache and target_pr is None else None
    return read_comment_history(
        now_epoch=now_epoch, reset_seconds=FACTORY_NO_DIFF_RETRY_RESET_SECONDS,
        scope=f'{REPO}:pr:{target_pr}' if target_pr is not None else REPO,
        cache_path=Path(cache) if cache else None, fetch_pages=fetch_pages, shared=shared,
    )


def load_no_diff_attempt_records(
    now_epoch: int | None = None, *, target_pr: int | None = None,
) -> list[NoDiffAttempt]:
    """Load durable retry history; isolate a targeted PR's own records."""
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    comments = recent_factory_comments(now_epoch, target_pr=target_pr)
    attempts = parse_no_diff_attempts_from_comments(comments, now_epoch=now_epoch)
    if target_pr is not None:
        return [attempt for attempt in attempts if attempt.kind == 'pr' and attempt.number == target_pr]
    return attempts


def load_no_diff_attempts(now_epoch: int | None=None) -> dict[int, int]:
    """Load durable rolling no-diff attempt counts for open issue ranking."""
    counts: dict[int, int] = {}
    for attempt in load_no_diff_attempt_records(now_epoch):
        counts[attempt.number] = counts.get(attempt.number, 0) + 1
    return counts


def unresolved_run_has_credible_liveness(run: dict[str, Any], *, now_epoch: int) -> bool:
    """Return whether an unresolved active-status run still merits a safety fence."""
    return factory_active_workers.run_is_live(run, repo=REPO, gh_json=lambda args: gh_json(args), now_epoch=now_epoch, ttl=FIXED_LEASE_TTL_SECONDS)


def active_fixed_workers() -> ActiveWorkerResult:
    """Return resolved workers and credible unresolved active-status runs."""
    return factory_active_workers.active_workers(REPO, gh_json=lambda args: gh_json(args), authorized=is_authorized_fixed_worker,
                                                 live=lambda run, now: unresolved_run_has_credible_liveness(run, now_epoch=now))


def owned_targets(issues: list[dict[str, Any]] | None=None, prs: list[dict[str, Any]] | None=None) -> list[tuple[int, str]]:
    """List open targets that currently have an active factory owner."""
    targets: list[tuple[int, str]] = []
    issue_items = list_issues() if issues is None else issues
    pr_items = list_prs() if prs is None else prs
    for item in [*issue_items, *pr_items]:
        owner = owner_of(labels_of(item))
        if owner and owner != 'factory:unowned':
            targets.append((int(item['number']), owner))
    return targets


def reconcile_contradictory_labels(
    issues: list[dict[str, Any]] | None=None,
    prs: list[dict[str, Any]] | None=None,
) -> list[int]:
    """Repair contradictory ownership and stage labels across all open targets.

     repairs:
    - `factory:building + factory:unowned` -> `factory:unowned` (no stage)
    - `ralph-status:in-progress + factory:unowned` -> `factory:unowned`
    - blocked labels (`ralph-status:blocked` and the rest of BLOCKED_LABELS)
      with no live factory lease are left alone
    - `ralph-status:done` with transient labels -> removed
    """
    issue_items = list_issues() if issues is None else issues
    pr_items = list_prs() if prs is None else prs
    repaired: list[int] = []
    from factory_work_policy import normalize_target_state
    
    for item in [*issue_items, *pr_items]:
        # Manual-only PRs are operator-managed. Do not manufacture
        # factory / factory:unowned / factory:blocked from a hold label.
        if is_manual_only(item.get("body")) or agent_hold(labels_of(item)):
            continue
        current_labels = labels_of(item)
        owner = owner_of(current_labels)
        # Operator holds such as ralph-status:blocked with no live factory:NN
        # lease must not be rewritten to factory:unowned + factory:blocked.
        if current_labels & BLOCKED_LABELS and (
            owner is None or owner == 'factory:unowned'
        ):
            continue

        norm_owner, norm_stage = normalize_target_state(current_labels, owner)
        
        # Check if normalization would actually change anything
        # We need to see if current labels match normalized state
        is_contradictory = False
        
        # 1. Check owner mismatch
        if owner != norm_owner:
            is_contradictory = True
        
        # 2. Check stage mismatch
        current_stage = stage_of(current_labels)
        if current_stage != norm_stage:
            is_contradictory = True

        # Special case: if it's unowned but still has a building label, it's contradictory
        if owner == 'factory:unowned' and 'factory:building' in current_labels:
            is_contradictory = True

        # An active owner may not coexist with factory:unowned (no-persisted-change
        # handoff can leave both behind); drop the redundant unowned marker.
        # Fail closed when two distinct active leases coexist with unowned:
        # guessing which owner is legitimate would silently drop a real lease.
        active_owners = {
            label for label in current_labels
            if OWNER_RE.fullmatch(label) and label != 'factory:unowned'
        }
        if len(active_owners) == 1 and 'factory:unowned' in current_labels:
            is_contradictory = True
        elif len(active_owners) > 1 and 'factory:unowned' in current_labels:
            is_contradictory = False

        # Executable work must never masquerade as in-progress without a real owner.
        if 'ralph-status:in-progress' in current_labels and owner == 'factory:unowned':
            is_contradictory = True
            
        if not is_contradictory:
            continue
            
        number = int(item['number'])
        # This will apply the normalized owner and stage
        if factory_crash.item(f'contradictory labels #{number}', replace_factory_labels, number, norm_owner, norm_stage) is factory_crash.SKIPPED:
            continue
        repaired.append(number)
        print(
            f'[factory-controller] repaired contradictory state on #{number}: {owner}/{current_stage} -> {norm_owner}/{norm_stage}',
            file=sys.stderr,
        )
    return repaired


def paired_owned_targets(
    issues: list[dict[str, Any]], prs: list[dict[str, Any]]
) -> dict[int, int]:
    """Find issue/PR pairs holding the same live owner lease.

    A local PR repair may update the PR without commenting on its issue.
    Treat that as activity for both halves of the same canonical work item.
    Ambiguous multi-PR links are ignored rather than guessing.
    """
    issue_map = {int(issue["number"]): issue for issue in issues}
    linked_by_issue: dict[int, list[int]] = {}
    for pr in prs:
        issue_number = linked_issue_from_pr(pr)
        if issue_number is None or issue_number not in issue_map:
            continue
        owner = owner_of(labels_of(pr))
        if owner in (None, "factory:unowned"):
            continue
        if owner != owner_of(labels_of(issue_map[issue_number])):
            continue
        linked_by_issue.setdefault(issue_number, []).append(int(pr["number"]))
    pairs: dict[int, int] = {}
    for issue_number, pr_numbers in linked_by_issue.items():
        if len(pr_numbers) != 1:
            continue
        pr_number = pr_numbers[0]
        pairs[issue_number] = pr_number
        pairs[pr_number] = issue_number
    return pairs


def _label_outlived_claim(worker: str, number: int) -> bool:
    """Return True only when the factory store proves the worker holds no claim on the target."""
    if not ledger_enabled():
        return False
    try:
        claim = worker_claim(REPO, worker)
    except Exception as exc:
        print(f'[factory-controller] claim check unavailable ({type(exc).__name__})', file=sys.stderr)
        return False
    return claim is None or number not in claim[1]


def reconcile_stale_leases(now_epoch: int | None = None) -> list[int]:
    """Expire inactive leases even when trusted progress markers are absent.

    GitHub updatedAt is the conservative liveness fallback for local sessions
    (and inactive fixed workers). Recheck the live owner and update time in the
    label writer to avoid taking over a newly refreshed lease.
    """
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    active_result = active_fixed_workers()
    if isinstance(active_result, tuple):
        active, unresolved_runs = active_result
    else:
        active, unresolved_runs = active_result, set()
    if unresolved_runs:
        print(
            '[factory-controller] retaining fixed leases because active run identity is unresolved: '
            + ', '.join(sorted(unresolved_runs)),
            file=sys.stderr,
        )
    try:
        factory_lease_owners.reconcile(REPO, lambda path: gh_json(['api', f'repos/{REPO}/{path}']), active_result, factory_state_ledger)
    except Exception as exc:
        print(f'[factory-controller] claim owner reconciliation deferred ({type(exc).__name__})', file=sys.stderr)
    issue_items, pr_items = list_issues(), list_prs()
    targets = {
        int(item["number"]): item
        for item in [*issue_items, *pr_items]
    }
    pairs = paired_owned_targets(issue_items, pr_items)
    released: list[int] = []
    prior_claims: dict[str, tuple[str, list[int]] | None] = {}
    released_workers: set[str] = set()
    for number, owner in owned_targets(issue_items, pr_items):
        # Manual-only holds cannot be changed by replace_factory_labels.
        # Avoid paginating their comment histories on every dispatch pass.
        if is_manual_only(targets[number].get('body')) or agent_hold(labels_of(targets[number])):
            continue
        fixed_owner = FIXED_OWNER_RE.fullmatch(owner)
        if fixed_owner and (unresolved_runs or int(fixed_owner.group("worker")) in active):
            continue
        paired_number = pairs.get(number)
        if fixed_owner and _label_outlived_claim(fixed_owner.group('worker'), number):
            # Assignment claims the factory store before labelling and every release clears
            # labels first, so a label with no claim is a failed release.
            if factory_crash.item(
                f'stale lease #{number}', replace_factory_labels, number, 'factory:unowned',
                expected_owner=owner,
                observed_at_epoch=now_epoch,
                related_owner_target=paired_number,
            ):
                released.append(number)
                print(
                    f'[factory-controller] released {owner} label on #{number}: no store claim',
                    file=sys.stderr,
                )
            continue
        marker_epoch = latest_lease_activity_epoch(number)
        # A malformed future timestamp cannot extend ownership indefinitely.
        if marker_epoch is not None and marker_epoch > now_epoch:
            marker_epoch = None
        updated_epoch = last_github_update_epoch(targets[number], now_epoch=now_epoch)
        if updated_epoch is None:
            # A partial list response must not turn an old *issue creation*
            # timestamp into evidence that a freshly claimed lease is stale.
            try:
                updated_epoch = last_github_update_epoch(
                    target_json(number), now_epoch=now_epoch
                )
            except RuntimeError:
                updated_epoch = None
        paired_update = (
            last_github_update_epoch(targets[paired_number], now_epoch=now_epoch)
            if paired_number is not None
            else None
        )
        observed = [
            epoch
            for epoch in (marker_epoch, updated_epoch, paired_update)
            if epoch is not None
        ]
        last_activity = max(observed) if observed else None
        if not lease_is_stale(
            owner,
            active_fixed_workers=active,
            has_unresolved_active_runs=bool(unresolved_runs),
            latest_activity_epoch=last_activity,
            now_epoch=now_epoch,
        ):
            continue
        ttl = FIXED_LEASE_TTL_SECONDS if fixed_owner else LOCAL_LEASE_TTL_SECONDS
        if fixed_owner:
            worker_id = fixed_owner.group('worker')
            if worker_id not in prior_claims:
                try:
                    prior_claims[worker_id] = worker_claim(REPO, worker_id)
                except Exception as exc:
                    prior_claims[worker_id] = None
                    print(f'[factory-controller] claim snapshot unavailable ({type(exc).__name__})', file=sys.stderr)
        if not factory_crash.item(
            f'stale lease #{number}', replace_factory_labels, number, 'factory:unowned',
            expected_owner=owner,
            inactive_before_epoch=now_epoch - ttl,
            observed_at_epoch=now_epoch,
            related_owner_target=paired_number,
        ):
            continue
        released.append(number)
        if fixed_owner:
            released_workers.add(fixed_owner.group('worker'))
        print(
            f'[factory-controller] released stale {owner} lease on #{number}',
            file=sys.stderr,
        )
    for worker_id in released_workers:
        snapshot = prior_claims.get(worker_id)
        if snapshot is None:
            continue
        token, claim_numbers = snapshot
        try:
            # Every target of the original token must have relinquished its
            # label. A newer store claim never matches this captured token.
            if all(
                not target_owned_by(number, f'factory:{worker_id}')
                for number in claim_numbers
            ):
                release_lease(REPO, worker_id, token, 'stale-lease-reconciled')
        except Exception as exc:
            print(f'[factory-controller] stale store release deferred ({type(exc).__name__})', file=sys.stderr)
    return released


def worker_has_active_lease(worker: str) -> bool:
    """Check this worker's GitHub ownership independently of candidate discovery."""
    if not worker.isdecimal():
        raise ValueError('invalid worker')
    rows = gh_json(['api', f'repos/{REPO}/issues?state=open&labels=factory%3A{worker}&per_page=1'])
    if not isinstance(rows, list):
        raise RuntimeError('worker ownership lookup unavailable')
    return bool(rows)


def omniroute_free_entry_cap() -> int:
    """Return the configured OmniRoute free-entry concurrency cap."""
    return env_positive_int('FACTORY_OMNIROUTE_FREE_ENTRY_CAP', DEFAULT_OMNIROUTE_FREE_ENTRY_CAP)


def in_flight_omniroute_free_entries() -> int:
    """Count Entry runs and equivalent leases occupying Entry capacity.

    Occupied units are the unique set of workers with a queued/in-progress
    Fixed Model Factory Entry or an active fixed-model lease. Unresolved
    numeric run identities each consume one extra unit. A failed run listing
    or non-numeric unresolved identity fails closed at the effective Entry
    cap (OmniRoute or multi-provider) so assignment cannot start additional
    sessions while occupancy is unknown.
    """
    cap = (
        omniroute_free_entry_cap()
        if omniroute_enabled()
        else DEFAULT_MULTI_PROVIDER_ENTRY_CAP
    )
    active_result = active_fixed_workers()
    workers, unresolved = active_result[0], active_result[1]
    if any(not item.isdigit() for item in unresolved):
        return cap
    leased: set[int] = set()
    for _, owner in owned_targets():
        match = FIXED_OWNER_RE.fullmatch(owner)
        if match:
            leased.add(int(match.group('worker')))
    return len(set(workers) | leased) + len(unresolved)


def omniroute_free_entry_capacity() -> dict[str, int]:
    """Return Entry occupancy for dispatch budgeting.

    OmniRoute enabled: OmniRoute free-pool cap.
    OmniRoute disabled (incident): multi-provider Entry budget so
    nvidia/opencode/openrouter/kilo can restore service.
    """
    enabled = omniroute_enabled()
    in_flight = in_flight_omniroute_free_entries()
    cap = omniroute_free_entry_cap() if enabled else DEFAULT_MULTI_PROVIDER_ENTRY_CAP
    return {
        'enabled': int(enabled),
        'in_flight': in_flight,
        'cap': cap,
        'remaining': remaining_omniroute_free_entry_slots(
            in_flight,
            cap=cap,
            enabled=enabled,
        ),
    }


def omniroute_free_entry_has_capacity() -> bool:
    """Return whether a new OmniRoute free Entry may start."""
    return omniroute_free_entry_capacity()['remaining'] > 0


def signal_completion_mode(*, now_epoch: int | None = None) -> dict[str, object]:
    """Signal dispatcher completion mode without directly assigning or launching.

    Completion drain wakes dispatcher completion mode and does not
    directly assign or launch Factory entries. Demand-driven completion
    allocation runs inside the dispatcher serialization boundary.
    """
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    reconcile_stale_leases(now_epoch=now_epoch)
    issues = list_issues()
    prs = list_prs()
    from factory_work_policy import factory_review_backlog_count
    backlog = factory_review_backlog_count(prs)
    snapshot = omniroute_free_entry_capacity()
    from factory_capacity_policy import completion_worker_target, FleetDemand
    demand = FleetDemand(
        completion=sum(
            1 for pr in prs
            if pr.get('state') == 'OPEN' and not pr.get('isDraft')
        ),
        production=len(issues),
        idle_workers=0,
    )
    target = completion_worker_target(demand)
    return {
        "signal": "completion",
        "backlog": backlog,
        "completion_target": target,
        "capacity": {
            'enabled': int(snapshot['enabled']),
            'in_flight': snapshot['in_flight'],
            'cap': snapshot['cap'],
            'remaining': snapshot['remaining'],
        },
        "assignments": [],
    }


def signal_roster_mode(*, now_epoch: int | None = None) -> dict[str, object]:
    """Signal roster mode for broad bootstrap dispatch.

    Broad bootstrap signals roster mode that cannot create a
    self-perpetuating roster chain.
    """
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    reconcile_stale_leases(now_epoch=now_epoch)
    snapshot = omniroute_free_entry_capacity()
    return {
        "signal": "roster",
        "capacity": {
            'enabled': int(snapshot['enabled']),
            'in_flight': snapshot['in_flight'],
            'cap': snapshot['cap'],
            'remaining': snapshot['remaining'],
        },
        "assignments": [],
    }


def runtime_quota_block_reason(worker: str) -> str | None:
    """Pause a shared free account until quota reset after a trusted quota error."""
    sources = worker_sources()
    source = sources.get(worker)
    if source not in {'z-ai', 'kilo-auto', 'mistral', 'ollama-cloud'}:
        return None
    # Share availability/cooldowns instead of reparsing thousands of comments
    # for every worker. On DB outage, retain the old trusted evidence path.
    try:
        from factory_state_operations import enabled as store_enabled, filter_workers
        if store_enabled():
            allowed, blocked = filter_workers(REPO, [worker])
            return None if allowed else "; ".join(blocked)
    except Exception as exc:
        print(f"[factory-controller] store health unavailable: {type(exc).__name__}", file=sys.stderr)
    # Provider status is mandatory; an outage is not permission to spend.
    return f"Factory {worker} deferred: shared provider health unavailable"


def is_authorized_fixed_worker(worker: str) -> bool:
    """Accept canonical worker IDs only when the roster actually contains them.

    Owner-label parsers deliberately recognize any positive decimal ID to
    preserve existing lease state, but assignment is restricted to the roster.
    """
    return (
        re.fullmatch(r'(?:[6-9]|[1-9][0-9]+)', worker) is not None
        and worker in worker_sources()
    )


def worker_skip_reason(worker: str, *, operator_force: bool = False, named: bool = False) -> str | None:
    """Return why this worker itself cannot take work now, or None.

    These checks are about the worker (switch, pause, credentials, lease, budget,
    entry capacity), not the shared work pool: a reason is not evidence of no work.
    Operator force skips the factory's own pacing (quota pauses, cooldowns, daily
    budgets, entry caps), never missing credentials or a running worker. A turned-off
    provider (factory_fleet_switch) holds even under force unless the request
    ``named`` that provider.
    """
    if not operator_force:
        quota_pause = quota_pause_for(worker)
        if quota_pause is not None:
            return describe_quota_pause(quota_pause)
        runtime_pause = runtime_quota_block_reason(worker)
        if runtime_pause is not None:
            return runtime_pause
    try:
        sources = worker_sources()
        source = sources.get(str(worker), "")
        from factory_fleet_switch import provider_reason
        if not named and (switched_off := provider_reason(source)):
            return f'Factory {worker}: {switched_off}'
        from factory_dispatch_credential_filter import ANONYMOUS, TRUTHY, source_requirements
        needs = source_requirements(source)
        if source not in ANONYMOUS and needs is not None:
            env = dict(os.environ)
            if not all(env.get(key, "").lower() in TRUTHY for key in needs):
                return f'Factory {worker} missing provider credentials'
        elif source and source not in ANONYMOUS and needs is None:
            # Unknown non-anonymous source: cannot prove credentials/configuration.
            return f'Factory {worker} has unknown provider configuration'
    except Exception as exc:
        # Never lease an unproven worker when credential eligibility cannot be evaluated.
        return f'credential eligibility unavailable for Factory {worker} ({type(exc).__name__})'
    if worker_has_active_lease(worker):
        return f'Factory {worker} already has an active lease'
    if operator_force:
        return None
    budget_reason = budget_block_reason(worker)
    if budget_reason is not None:
        return budget_reason
    snapshot = omniroute_free_entry_capacity()
    if snapshot['remaining'] <= 0:
        return (
            'OmniRoute free-entry cap reached '
            f'(in_flight={snapshot["in_flight"]} cap={snapshot["cap"]})'
        )
    return None


def assign(worker: str, kinds: tuple[str, ...] | None = None,
    *,
    target: tuple[str, int] | None = None,
    requested_action: str = 'advance',
    batch_reconciled: bool = False,
    operator_force: bool = False,
    named: bool = False,
) -> Candidate | None:
    """Assign the highest-ranked executable work to one fixed-model worker.

    When kinds is provided, only candidates whose kind is in the tuple
    are considered. This allows completion drains to request dispatcher
    allocation restricted to specific candidate types.

    Dispatch failures release any lease created by the dispatcher and
    do not strand ownership.
    """
    if not is_authorized_fixed_worker(worker):
        raise SystemExit(f'unsupported fixed-model worker: {worker}')
    skip_reason = worker_skip_reason(worker, operator_force=operator_force, named=named)
    if skip_reason is not None:
        print(f'[factory-controller] {skip_reason}; skipping dispatch', file=sys.stderr)
        return None
    if operator_force and target is not None:
        return assign_forced_target(worker, target, requested_action)
    return assign_from_pool(worker, kinds, target=target, requested_action=requested_action,
                            batch_reconciled=batch_reconciled, operator_force=operator_force)


def ready_pool(issues: list[dict[str, Any]], prs: list[dict[str, Any]], attempt_records: list[NoDiffAttempt] | None, *, kinds: tuple[str, ...] | None = None, target: tuple[str, int] | None = None, operator_force: bool = False) -> factory_ready.Ready:
    """The ranked pool this pass walks; the ready command prints the same list."""
    global EXTERNAL_WIP_SEEN
    try:
        external_wip = external_wip_count(REPO) if ledger_enabled() else 0
    except Exception as exc:
        print(f'[factory-controller] external lease capacity unknown ({type(exc).__name__}); holding new intake', file=sys.stderr)
        external_wip = FACTORY_PR_WIP_LIMIT
    EXTERNAL_WIP_SEEN = external_wip
    # A PR-only target never leases new issue work, so it skips the intake decision.
    intake = (rotisserie_issue_intake(issues, prs, operator_force=True) if operator_force else rotisserie_issue_intake(issues, prs)) if target is None or target[0] == 'issue' else None
    # Retry cooldown and the loop breaker never hold a manual target or a forced run.
    holds = factory_dispatch_holds.reader(REPO, prs) if target is None and not operator_force else None
    return factory_ready.pool(issues, prs, attempt_records=attempt_records, external_wip=external_wip, intake=intake, links=factory_deps.stored(REPO), holds=holds, kinds=kinds, operator_force=operator_force, intake_holds=target is None, rank=build_candidates)


def ready_inputs() -> tuple[list[dict[str, Any]], list[dict[str, Any]], factory_ready.Ready]:
    issues = list_issues()
    prs = factory_dispatch_holds.stamp(REPO, list_prs(), issues)
    try:
        records: list[NoDiffAttempt] | None = load_no_diff_attempt_records()
    except RuntimeError:
        records = None
    return issues, prs, ready_pool(issues, prs, records)


def assign_from_pool(worker: str, kinds: tuple[str, ...] | None = None,
    *,
    target: tuple[str, int] | None = None,
    requested_action: str = 'advance',
    batch_reconciled: bool = False,
    operator_force: bool = False,
    named: bool = False,
) -> Candidate | None:
    """Rank the shared pool for a worker already proven eligible."""
    trace.reset()
    # Trusted dispatcher batches run these two full-repository reconciliations
    # once before iterating workers. Standalone/manual assignment still checks
    # them here. Every actual claim remains live-verified in assign_candidate.
    if target is None and not batch_reconciled:
        reconcile_stale_leases()
        reconcile_contradictory_labels()
    issues = list_issues()
    prs = factory_dispatch_holds.stamp(REPO, list_prs(), issues)
    # Resolve an issue wakeup to its canonical open PR *before* loading retry history or asking
    # Rotisserie to rank fresh issues; otherwise every worker reads the whole retry window.
    if target is not None and target[0] == 'issue':
        issue_number = target[1]
        linked_prs = [
            pr for pr in prs
            if linked_issue_from_pr(pr) == issue_number
            and pr_suppresses_issue_candidate(pr, {})
        ]
        if len(linked_prs) > 1:
            raise RuntimeError('Multiple open PRs claim this issue. Select the PR directly.')
        if linked_prs:
            target = ('pr', int(linked_prs[0]['number']))
    try:
        # A targeted PR wakeup needs only that PR's retry evidence; a full pass needs the window.
        attempt_records: list[NoDiffAttempt] | None = (
            load_no_diff_attempt_records(target_pr=target[1])
            if target is not None and target[0] == 'pr'
            else load_no_diff_attempt_records()
        )
    except RuntimeError as exc:
        attempt_records = None
        print(f'[factory-controller] no-diff retry history unavailable; holding fresh issue intake: {exc}', file=sys.stderr)
    candidates = list(ready_pool(issues, prs, attempt_records, kinds=kinds, target=target, operator_force=operator_force).candidates)
    if target is None:
        factory_issue_intake_guard.request_splits_gh(issues, REPO)
    if target is not None:
        kind, number = target
        candidates = [
            candidate for candidate in candidates
            if (candidate.kind, candidate.number) == (kind, number)
        ]
        if not candidates:
            raise RuntimeError('No eligible work on this target. Check its stage, leases and blockers.')
        selected = candidates[0]
        eligible_action = (
            'implement' if selected.kind == 'issue'
            else completion_action_for(selected.stage, selected.conflicted)
        )
        ci_override = (
            requested_action == 'ci-repair' and eligible_action == 'review-findings'
            and required_checks_failed(selected.number)
        )
        if not ci_override and (
            eligible_action is None or requested_action not in ('advance', eligible_action)
        ):
            raise RuntimeError(f'Requested action is unavailable. Current action: {eligible_action or "none"}.')
        if not candidate_is_live_executable(selected):
            raise RuntimeError('The selected target is blocked or still waiting on checks.')
    # Operator-forced work is an explicit owner request, so advisory retry
    # cooldown and soft queue backpressure may not suppress all available work.
    # Hard candidate, dependency, ownership and live-claim checks remain active.
    if operator_force and target is None:
        print('[factory-controller] operator force: bypassing advisory retry cooldown and soft backlog pressure', file=sys.stderr)
    candidates = order_candidates_for_worker(
        resolve_review_head_contributors(candidates, prs), worker
    )
    # The dispatcher prepares one bounded Jev decision before leasing any
    # worker. Completion actions may be promoted across ordinary lane-3 PR
    # classes; every live claim, protected lane, and worker-specific exclusion
    # remains authoritative. Legacy same-class issue reports still apply.
    jev_path = os.environ.get("FACTORY_JEV_RANKING_FILE", "") if target is None else ""
    canonical_candidates = candidates
    jev_used = False
    jev_pr: int | None = None
    jev_action: str | None = None
    jev_head: str | None = None
    if jev_path:
        try:
            with open(jev_path, encoding="utf-8") as jev_file:
                jev_report = json.load(jev_file)
            if not isinstance(jev_report, dict):
                raise ValueError("invalid Jev decision report")
            baseline_order = [c.number for c in candidates]
            if jev_report.get("surface") == "pr-completion":
                heads = {
                    int(pr["number"]): str(pr.get("headRefOid") or "")
                    for pr in prs
                    if "number" in pr
                }
                candidates, jev_used, jev_reason = apply_completion_ranking(
                    candidates, jev_report, heads
                )
                chosen = jev_report.get("chosen")
                entry = (jev_report.get("action_map") or {}).get(chosen, {})
                if jev_used and isinstance(entry, dict):
                    jev_pr = entry.get("pr")
                    jev_action = entry.get("action")
                    jev_head = entry.get("head")
                if jev_used:
                    print(
                        f"[factory-controller] Jev ACTIVE surface=pr-completion "
                        f"worker={worker}: chosen={chosen} action={jev_action} "
                        f"canonical={baseline_order[:8]} chosen-order="
                        f"{[c.number for c in candidates[:8]]}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[factory-controller] Jev NOT APPLIED "
                        f"surface=pr-completion worker={worker}: "
                        f"status={jev_report.get('status')} "
                        f"reason={jev_reason} detail={jev_report.get('reason')}",
                        file=sys.stderr,
                    )
            else:
                candidates, jev_used = apply_jev_ranking(candidates, jev_report)
                if jev_used:
                    print(
                        f"[factory-controller] Jev ACTIVE worker={worker}: "
                        f"canonical={baseline_order[:8]} chosen-order="
                        f"{[c.number for c in candidates[:8]]}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[factory-controller] Jev NOT APPLIED "
                        f"surface=issue-selection worker={worker}: "
                        f"status={jev_report.get('status')} "
                        f"reason={jev_report.get('reason')}",
                        file=sys.stderr,
                    )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            print(
                f"[factory-controller] Jev unavailable ({type(exc).__name__}); "
                "using canonical order",
                file=sys.stderr,
            )
    index = 0
    fallback_used = False
    while index < len(candidates):
        candidate = candidates[index]
        index += 1
        if not candidate_is_live_executable(candidate):
            trace.skip(candidate.number, 'blocked, ready, or waiting on CI')
            continue
        if candidate.kind == "issue" and issue_excludes_worker_on_strike_retry(candidate.number, worker):
            trace.skip(candidate.number, f'strike retry excludes Factory {worker}')
            continue
        action = jev_action if jev_used and jev_pr == candidate.number else None
        if target is not None:
            if candidate.kind == 'pr':
                head = next(str(pr.get('headRefOid') or '') for pr in prs if pr['number'] == candidate.number)
                if not re.fullmatch(r'[0-9a-f]{40}', head):
                    continue
                if ci_override:
                    if assign_candidate(
                        candidate, worker, action='ci-repair', expected_head=head, source='manual'
                    ) and factory_dispatch_holds.released(REPO, candidate, prs):
                        return candidate
                elif assign_candidate(candidate, worker, expected_head=head, source='manual') and factory_dispatch_holds.released(REPO, candidate, prs):
                    return candidate
                continue
            if assign_candidate(candidate, worker, source='manual'):
                return candidate
            continue
        # Issue-selection provenance: record source=jev only when the actual
        # pick differs from the deterministic first live candidate, so the
        # durable lease never misattributes a canonical choice to the model.
        canonical_issue: Candidate | None = None
        jev_source: str | None = None
        if action is None and jev_used and candidate.kind == "issue":
            canonical_issue = first_live_candidate(canonical_candidates, worker)
            if canonical_issue is None or canonical_issue.number != candidate.number:
                jev_source = "jev"
        if action is not None:
            assigned = assign_candidate(
                candidate,
                worker,
                action=action,
                expected_head=jev_head,
            )
        elif jev_source is not None:
            assigned = assign_candidate(candidate, worker, source=jev_source)
        else:
            assigned = assign_candidate(candidate, worker)
        if assigned and (not operator_force or factory_dispatch_holds.released(REPO, candidate, prs)):
            reason = ' conflict-repair' if candidate.conflicted else ''
            print(f'[factory-controller] assigned {candidate.kind} #{candidate.number} lane={candidate.lane} priority={candidate.priority}{reason} to Factory {worker}', file=sys.stderr)
            if action is not None:
                log_completion_result(candidate, canonical_candidates, worker, action)
            elif jev_used and candidate.kind == "issue":
                baseline = (
                    "none" if canonical_issue is None
                    else f"#{canonical_issue.number}"
                )
                print(
                    f"[factory-controller] Jev RESULT worker={worker} "
                    f"assigned=issue #{candidate.number} canonical={baseline} "
                    f"influenced={'yes' if jev_source is not None else 'no'}",
                    file=sys.stderr,
                )
            return candidate
        if action is not None and not fallback_used:
            fallback_used = True
            print(
                f"[factory-controller] Jev FALLBACK worker={worker}: chosen "
                f"PR #{candidate.number} was rejected at lease time; "
                "reselecting from canonical order",
                file=sys.stderr,
            )
            jev_used = False
            candidates = canonical_candidates
            index = 0
    return None


def first_live_candidate(
    candidates: list[Candidate], worker: str
) -> Candidate | None:
    """Return the candidate the deterministic controller would lease next.

    The scan is bounded to the decision window so influence accounting cannot
    turn one assignment into an unbounded API walk.
    """
    for candidate in candidates[:8]:
        if not candidate_is_live_executable(candidate):
            continue
        if (
            candidate.kind == "issue"
            and issue_excludes_worker_on_strike_retry(candidate.number, worker)
        ):
            continue
        return candidate
    return None


def log_completion_result(
    assigned: Candidate,
    canonical: list[Candidate],
    worker: str,
    action: str,
) -> None:
    """Record whether the promoted action changed the deterministic lease."""
    baseline = first_live_candidate(canonical, worker)
    influenced = baseline is None or baseline.number != assigned.number
    print(
        f"[factory-controller] Jev RESULT worker={worker} assigned=pr "
        f"#{assigned.number} action={action} "
        f"canonical=#{baseline.number if baseline is not None else 'none'} "
        f"influenced={'yes' if influenced else 'no'}",
        file=sys.stderr,
    )


def _active_claim(worker: str, number: int, claim_id: object = '') -> tuple[str, list[int]] | None:
    """Without the factory store the GitHub owner label is the lease; the caller checks it.

    A handoff that carries a claim ID was issued through the factory store and is always
    checked against the factory store.
    """
    if not claim_id and not ledger_enabled() and os.environ.get('FACTORY_STATE_REQUIRED') != 'true':
        return '', [number]
    return worker_claim(REPO, worker, active_only=True)


def assignment_context(candidate: Candidate, worker: str) -> dict[str, object]:
    """Export a small, fenced assignment after a successful dispatcher claim."""
    claim = _active_claim(worker, candidate.number)
    if claim is None or candidate.number not in claim[1]:
        raise RuntimeError('Claim disappeared before assignment handoff')
    context: dict[str, object] = {
        'kind': candidate.kind, 'number': candidate.number,
        'worker': worker, 'claim_id': claim[0],
        'stage': candidate.stage or 'factory:building',
        'linked_issue': candidate.linked_issue,
        'head': '', 'branch': '',
    }
    if candidate.kind == 'pr':
        live = live_pr_state(candidate.number)
        context['stage'] = stage_of(labels_of(live))
        context['head'] = live['head']['sha']
        context['branch'] = live['head']['ref']
    return context


def validate_assignment(worker: str, raw: str) -> dict[str, object]:
    """Verify dispatcher context against the live claim and exact GitHub target."""
    if len(raw.encode()) > 4096:
        raise ValueError('Assignment exceeds bounded input size')
    context = json.loads(raw)
    if not isinstance(context, dict) or context.get('worker') != worker:
        raise ValueError('Assignment worker mismatch')
    kind, number = context.get('kind'), context.get('number')
    if kind not in ('issue', 'pr') or type(number) is not int or number <= 0:
        raise ValueError('Invalid assignment target')
    claim = _active_claim(worker, number, context.get('claim_id'))
    if claim is None or claim[0] != context.get('claim_id') or number not in claim[1]:
        raise ValueError('Assignment claim expired or was replaced')
    target = target_json(number)
    if target.get('state') != 'open' or owner_of(labels_of(target)) != f'factory:{worker}':
        raise ValueError('Assignment target is no longer owned')
    if kind == 'pr':
        live = live_pr_state(number)
        if live['head']['sha'] != context.get('head') or live['head']['ref'] != context.get('branch'):
            raise ValueError('Assignment PR head moved')
        if stage_of(labels_of(live)) != context.get('stage'):
            raise ValueError('Assignment PR stage changed')
        # Match dispatch-time eligibility against the *live* merge state.
        # Reconstructing a Candidate without conflicted=True previously
        # rejected the very repair job that dispatch had just leased.
        live_conflicted = (
            live.get('mergeable') is False
            or str(live.get('mergeable_state') or '').upper() == 'DIRTY'
        )
        candidate = Candidate(
            'pr', number, 3, 0, '',
            stage=str(context.get('stage') or ''),
            conflicted=live_conflicted,
        )
        if not candidate_is_live_executable(candidate) and not operator_forced_claim(number, worker):
            raise ValueError('Assignment PR is waiting or no longer executable')
    return context | ({'operator_forced': operator_forced_claim(number, worker)} if context.get('operator_forced') is True else {})


def inspect_assignment(worker: str) -> dict[str, Any]:
    """Return the current controller assignment for one fixed-model worker.

    Dispatcher claims before Entry starts, so smoke can inspect the leased
    target so exact-head review can use the multi-provider Entry path while
    OmniRoute is dark (instead of smoking OmniRoute ``auto/*`` routes).
    """
    owner = f'factory:{worker}'
    issues = list_issues()
    prs = list_prs()
    owned_prs = [item for item in prs if owner_of(labels_of(item)) == owner]
    owned_issues = [item for item in issues if owner_of(labels_of(item)) == owner]
    if owned_prs:
        item = owned_prs[0]
        labels = labels_of(item)
        stage = next((name for name in STAGE_PRECEDENCE if name in labels), 'factory:review')
        return {
            'kind': 'pr',
            'number': int(item['number']),
            'stage': stage,
            'owner': owner,
        }
    if owned_issues:
        item = owned_issues[0]
        labels = labels_of(item)
        stage = next((name for name in STAGE_PRECEDENCE if name in labels), 'factory:building')
        return {
            'kind': 'issue',
            'number': int(item['number']),
            'stage': stage,
            'owner': owner,
        }
    return {'kind': 'none'}


def record_claim_released(number: int, worker: str, kind: str, reason: str) -> None:
    """Post a trusted claim-released marker for one controller lease."""
    epoch = int(time.time())
    worker_id = f'opencode-free-model-factory-{worker}'
    marker = (
        f'<!-- {MARKER_PREFIX}-factory-claim-released-v3:'
        f'{kind}-{number}:{worker_id}:{epoch}:{reason} -->'
    )
    run_gh(['issue', 'comment', str(number), '--repo', REPO, '--body', marker])


# GitHub installation rate limits and 5xx responses are transient. Trusted
# exit cleanup retries them with a bounded backoff (about 22s in total, well
# inside the 5-minute release job) instead of stranding the lease until TTL.
TRANSIENT_GITHUB_RE = re.compile(
    r"rate limit|abuse detection|HTTP (?:429|50[0-9])|\b(?:502|503|504)\b|"
    r"timed out|connection reset|unexpected EOF|temporarily unavailable",
    re.I,
)
GITHUB_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 15.0)


def _retry_transient_github[T](operation: Callable[[], T]) -> T:
    """Run one GitHub read/write, retrying only transient rate-limit/5xx errors."""
    for delay in (*GITHUB_RETRY_DELAYS, None):
        try:
            return operation()
        except RuntimeError as exc:
            if delay is None or not TRANSIENT_GITHUB_RE.search(str(exc)):
                raise
            print(f'[factory-controller] transient GitHub failure; retrying in {delay:g}s', file=sys.stderr)
            time.sleep(delay)
    raise AssertionError('unreachable')


def release_worker(worker: str, reason: str = 'controller-release', *, details=None, expected_claim: str = '') -> list[int]:
    """Release an exiting worker's exact store claim, never a later assignment.

    Trusted pre/post-session cleanup uses the original transactional claim as
    the target inventory; a potentially stale issue/PR projection is not an
    ownership authority. GitHub is re-read at the label mutation boundary and
    the claim UUID is checked before every write and on transactional release.
    """
    owner = f'factory:{worker}'
    trusted_exit = reason.startswith(('pre-session-abort-run-', 'post-session-exit-run-'))
    try:
        original_claim = worker_claim(REPO, worker)
    except Exception as exc:
        print(f'[factory-controller] store release snapshot unavailable ({type(exc).__name__})', file=sys.stderr)
        if trusted_exit:
            raise
        original_claim = None

    if trusted_exit and original_claim is not None and not expected_claim:
        if details is not None:
            details.update({'status': 'missing_claim', 'released': []})
        return []
    if trusted_exit and expected_claim and (
        original_claim is None or original_claim[0] != expected_claim
    ):
        if details is not None:
            details.update({'status': 'stale_completion', 'released': []})
        return []

    released: list[int] = []
    if trusted_exit:
        already_released: list[int] = []
        deferred: list[int] = []
        failed: list[str] = []
        labels_pending: list[int] = []
        claim_released = False
        if original_claim is not None:
            token, claim_numbers = original_claim
            for number in sorted(set(claim_numbers)):
                try:
                    # authoritative GitHub, not store discovery
                    current = _retry_transient_github(lambda n=number: target_json(n))
                    active_owners = {
                        label['name'] for label in current.get('labels', [])
                        if OWNER_RE.fullmatch(label.get('name', ''))
                        and label['name'] != 'factory:unowned'
                    }
                    if active_owners != {owner}:
                        # Model-side releases can precede trusted cleanup and
                        # lack a webhook. Refresh only truly unowned targets.
                        if not active_owners:
                            refresh_discovery_after_label_change(number, current)
                        # Never overwrite ownership assigned to a newer worker.
                        already_released.append(number)
                        continue
                    if worker_claim(REPO, worker) != original_claim:
                        deferred.append(number)  # newer claim or disconnected ledger
                        continue
                    # This call itself re-reads live labels with expected_owner,
                    # just before the single atomic GitHub label-set mutation.
                    if not _retry_transient_github(lambda n=number: replace_factory_labels(
                        n, 'factory:unowned', expected_owner=owner
                    )):
                        # A retried write may have landed before its response
                        # was lost; then the owner guard refuses the repeat.
                        if not _retry_transient_github(lambda n=number: target_owned_by(n, owner)):
                            already_released.append(number)
                        else:
                            deferred.append(number)
                        continue
                    released.append(number)
                    kind = 'pr' if 'pull_request' in current else 'issue'
                    try:
                        record_claim_released(number, worker, kind, reason)
                    except Exception as exc:
                        # The ownership write is done. Retryable marker failure
                        # must not strand the transactional lease.
                        print(f'[factory-controller] release marker #{number} unavailable ({type(exc).__name__})', file=sys.stderr)
                except Exception as exc:
                    failed.append(f'{number}:{type(exc).__name__}')
            labels_pending = [
                int(item.split(':', 1)[0]) for item in failed
                if item.split(':', 1)[0].isdigit()
            ]
            if labels_pending and not deferred and len(labels_pending) == len(failed):
                # GitHub stayed unavailable after bounded retries. The worker
                # has exited, so discharge its exact store claim anyway rather
                # than holding capacity until the TTL. Labels left on these
                # targets keep the worker busy for dispatch (GitHub ownership
                # is checked separately) until stale-lease reconciliation runs.
                try:
                    claim_released = release_lease(REPO, worker, token, reason)
                    if not claim_released and worker_claim(REPO, worker) is None:
                        claim_released = True
                except Exception as exc:
                    failed.append(f'neon_release:{type(exc).__name__}')
            if not deferred and not failed:
                try:
                    # Only discharge the exact original UUID when ALL its
                    # linked GitHub labels are verified no longer owned.
                    if all(
                        not _retry_transient_github(lambda n=number: target_owned_by(n, owner))
                        for number in claim_numbers
                    ):
                        claim_released = release_lease(REPO, worker, token, reason)
                        if not claim_released:
                            later = worker_claim(REPO, worker)
                            if later is None:
                                # Idempotent retry: a parallel release won.
                                claim_released = True
                            elif later != original_claim:
                                deferred.extend(claim_numbers)
                            else:
                                failed.append('neon_release_rejected')
                    else:
                        deferred.extend(claim_numbers)
                except Exception as exc:
                    failed.append(f'neon_release:{type(exc).__name__}')
        # No token on a trusted retry is a safe no-op. Do not scan unrelated
        # labels and accidentally strip a reassigned worker.
        status = (
            'failed' if failed else
            'deferred' if deferred else
            'released' if claim_released else
            'already_released'
        )
        if status == 'failed' and claim_released:
            status = 'labels_pending'
        summary = {
            'status': status,
            'claim_id': original_claim[0] if original_claim else None,
            'claim_released': claim_released,
            'released': released,
            'already_released': already_released,
            'deferred': sorted(set(deferred)),
            'failed': failed,
            'labels_pending': labels_pending,
        }
        if details is not None:
            details.update(summary)
        print(f'[factory-controller] cleanup {json.dumps(summary, sort_keys=True)}', file=sys.stderr)
        return released

    # Legacy/operator release path. Trusted worker exits never rely on this
    # potentially stale discovery snapshot.
    targets: list[tuple[int, str]] = []
    for item in list_issues():
        if owner_of(labels_of(item)) == owner:
            targets.append((int(item['number']), 'issue'))
    for item in list_prs():
        if owner_of(labels_of(item)) == owner:
            targets.append((int(item['number']), 'pr'))
    for number, kind in targets:
        if factory_crash.item(f'release #{number}', replace_factory_labels, number, 'factory:unowned') in (False, factory_crash.SKIPPED):
            continue
        try:
            record_claim_released(number, worker, kind, reason)
        except Exception:
            pass
        released.append(number)
    if original_claim is not None and len(released) == len(targets):
        token, claim_numbers = original_claim
        if all(not target_owned_by(number, owner) for number in claim_numbers):
            release_lease(REPO, worker, token, reason)
    return released

def selectivity_conflict_recovery(worker: str) -> dict[str, Any]:
    """Perform selective lease recovery for dual-lease incidents.
    
    When a worker owns one canonical PR plus an unrelated accidental issue lease,
    keep the PR and release only the issue. When an orphan issue lease has an
    existing canonical PR, release the orphan issue without disturbing the PR.
    Before releasing, verify the worker still owns the target. Ambiguous
    multi-PR/multi-issue corruption fails closed rather than guessing.
    
    Returns a dict with recovery results and any error messages.
    """
    owner = f'factory:{worker}'
    issues = list_issues()
    prs = list_prs()
    
    # Find all targets owned by this worker
    owned_issues = []
    owned_prs = []
    for item in issues:
        if owner_of(labels_of(item)) == owner:
            owned_issues.append(item)
    for item in prs:
        if owner_of(labels_of(item)) == owner:
            owned_prs.append(item)
    
    result = {
        'worker': worker,
        'recovered_issues': [],
        'recovered_prs': [],
        'canonical_prs_preserved': [],
        'orphan_issues_released': [],
        'ambiguous_cases': [],
        'errors': []
    }
    
    # Case 3: Check for ambiguous multi-PR/multi-issue corruption first - fail closed
    if len(owned_issues) > 1 or len(owned_prs) > 1:
        # Check for complex patterns that we can't safely resolve
        complex_patterns = []
        
        # Multiple PRs with different linked issues
        if len(owned_prs) > 1:
            pr_linked_issues = set()
            for pr in owned_prs:
                linked = linked_issue_from_pr(pr)
                if linked:
                    pr_linked_issues.add(linked)
            if len(pr_linked_issues) > 1:
                complex_patterns.append('multiple PRs with different linked issues')
        
        # Multiple issues where some could be linked to PRs not owned by this worker
        if len(owned_issues) > 1:
            issue_pr_links = {}
            for issue in owned_issues:
                issue_num = int(issue['number'])
                issue_pr_links[issue_num] = []
                
                for pr in prs:
                    if linked_issue_from_pr(pr) == issue_num:
                        issue_pr_links[issue_num].append(int(pr['number']))
            
            # Check if any issue could be claimed by multiple PRs
            for issue_num, linked_prs in issue_pr_links.items():
                if len(linked_prs) > 1:
                    complex_patterns.append(f'issue {issue_num} claimed by multiple PRs')
        
        # Check for individual ambiguous cases
        for issue in owned_issues:
            issue_number = int(issue['number'])
            # No canonical PR found for this issue, check if it's ambiguous
            other_worker_prs = [
                p for p in prs
                if linked_issue_from_pr(p) == issue_number and owner_of(labels_of(p)) != owner
            ]
            if other_worker_prs:
                # This might be ambiguous - multiple workers could claim this issue
                result['ambiguous_cases'].append({
                    'issue': issue_number,
                    'reason': 'multiple workers could claim this issue'
                })
        
        if complex_patterns:
            result['ambiguous_cases'].extend([{
                'pattern': pattern,
                'reason': 'complex multi-PR/multi-issue corruption - failing closed'
            } for pattern in complex_patterns])
        
        # If any ambiguous cases found, fail closed - do not perform any recovery
        if result['ambiguous_cases']:
            return result
    
    # Case 1: Worker owns canonical PR + unrelated accidental issue lease
    for pr in owned_prs:
        pr_number = int(pr['number'])
        
        # Check if this PR is canonical (has linked issue)
        linked_issue = linked_issue_from_pr(pr)
        if linked_issue is None:
            continue  # Not a canonical PR
            
        # Find any unrelated issue leases owned by the same worker
        for issue in owned_issues:
            issue_number = int(issue['number'])
            
            # Skip if this issue is the linked issue of the PR
            if issue_number == linked_issue:
                continue
                
            # Verify the worker still owns both targets
            if not (target_owned_by(issue_number, owner) and target_owned_by(pr_number, owner)):
                continue
                
            # This is a canonical PR + unrelated accidental issue lease
            # Release only the issue, preserve the PR
            try:
                replace_factory_labels(issue_number, 'factory:unowned')
                record_claim_released(issue_number, worker, 'issue', 'selective-conflict-recovery')
                result['recovered_issues'].append(issue_number)
                result['canonical_prs_preserved'].append(pr_number)
                result['orphan_issues_released'].append(issue_number)
            except Exception as e:
                result['errors'].append(f'Failed to release issue {issue_number}: {str(e)}')
    
    # Case 2: Orphan issue lease + existing canonical PR
    for issue in owned_issues:
        issue_number = int(issue['number'])
        
        # Find any canonical PR that would claim this issue
        linked_pr_found = False
        for pr in prs:
            pr_number = int(pr['number'])
            
            # Check if this PR would claim the issue (via branch or explicit close)
            pr_linked_issue = linked_issue_from_branch(pr.get('headRefName', ''))
            pr_closes_issue = issue_explicitly_closed_by_pr(pr)
            
            if pr_linked_issue == issue_number or pr_closes_issue == issue_number:
                # Verify the PR is still open and not owned by this worker
                if (str(pr.get('state', 'OPEN')).upper() == 'OPEN' and 
                    not pr.get('isDraft') and
                    owner_of(labels_of(pr)) != owner):
                    
                    # Verify the worker still owns the issue
                    if not target_owned_by(issue_number, owner):
                        continue
                        
                    # This is an orphan issue lease + existing canonical PR
                    # Release only the issue, preserve the PR
                    try:
                        replace_factory_labels(issue_number, 'factory:unowned')
                        record_claim_released(issue_number, worker, 'issue', 'selective-conflict-recovery')
                        result['recovered_issues'].append(issue_number)
                        result['orphan_issues_released'].append(issue_number)
                        linked_pr_found = True
                        break
                    except Exception as e:
                        result['errors'].append(f'Failed to release orphan issue {issue_number}: {str(e)}')
        
        if not linked_pr_found:
            # No canonical PR found for this issue, check if it's ambiguous
            other_worker_prs = [
                p for p in prs
                if linked_issue_from_pr(p) == issue_number and owner_of(labels_of(p)) != owner
            ]
            if other_worker_prs:
                # This might be ambiguous - multiple workers could claim this issue
                result['ambiguous_cases'].append({
                    'issue': issue_number,
                    'reason': 'multiple workers could claim this issue'
                })
    
    return result


def explain_issue(
    number: int,
    *,
    issue: dict[str, Any] | None = None,
    issues: list[dict[str, Any]] | None = None,
    prs: list[dict[str, Any]] | None = None,
    no_diff_attempts_by_issue: dict[int, int] | None = None,
    include_dynamic_gates: bool = True,
) -> dict[str, Any]:
    """Explain why one issue is or is not currently queue-eligible.

    Answers the operator question documented in
    ``docs/FACTORY_QUEUE_ELIGIBILITY.md`` without requiring anyone to read the
    selection code or interpret historical claim comments by hand. The static
    verdict comes from the shared eligibility module used by both selectors, so
    the report cannot drift from what dispatch actually enforces; the
    controller-only dynamic gates are appended after it.

    Args:
        number: The issue number to explain.
        issue: Pre-fetched issue payload; fetched on demand when omitted.
        issues: Pre-fetched issue rows used for dependency resolution.
        prs: Pre-fetched open-PR rows used for canonical-PR suppression.
        no_diff_attempts_by_issue: Rolling no-diff retry counts by issue.
        include_dynamic_gates: Whether to evaluate the live GitHub gates.

    Returns:
        A JSON-serializable report with the verdict, its reasons, the canonical
        owning PR when one exists, and the dynamic gate results.
    """
    issue_rows = list_issues() if issues is None else list(issues)
    pr_rows = factory_dispatch_holds.stamp(REPO, list_prs() if prs is None else list(prs), issue_rows)
    payload = issue
    if payload is None:
        payload = next((row for row in issue_rows if int(row['number']) == number), None)
    if payload is None:
        payload = target_json(number)
        issue_rows = [*issue_rows, payload]
    counts = (
        load_no_diff_attempts() if no_diff_attempts_by_issue is None else no_diff_attempts_by_issue
    )
    return explain_issue_report(
        number,
        payload,
        issues=issue_rows,
        prs=pr_rows,
        no_diff_attempts=counts,
        intake=rotisserie_issue_intake(issue_rows, pr_rows),
        open_blocker=issue_has_open_blocker(number) if include_dynamic_gates else None,
    )


def check_stale_prs(dry_run: bool = False) -> dict[str, Any]:
    """Report stale open PRs for recovery without discarding their work.

    Returns the baseline and non-destructive diagnostic results.
    """
    guard = StalePRGuard()
    # The staleness baseline includes *merged* Factory PRs; otherwise the
    # cold-start 24h fallback reports misleadingly stale work. No stale-age
    # result authorizes closing an unfinished PR or resetting its issue.
    prs = list_factory_prs()
    baseline = guard.get_baseline(prs)
    results = guard.process_stale_prs(prs=prs, dry_run=dry_run)
    preserved = [r for r in results if r.get("status") == "stale-preserved"]
    return {
        "baseline": baseline,
        "results": results,
        "expired_count": 0,
        "stale_preserved_count": len(preserved),
        "dry_run": dry_run,
    }


class TargetBusy(RuntimeError):
    """An operator target is held by another worker; report, never steal."""

    def __init__(self, number: int, owner: str) -> None:
        super().__init__(f'#{number} is being worked on by {owner}')
        self.number = number
        self.owner = owner


def _owners(labels: set[str]) -> set[str]:
    return {label for label in labels if OWNER_RE.fullmatch(label) and label != 'factory:unowned'}


def forced_target_candidate(kind: str, number: int, requested_action: str = 'advance') -> tuple[Candidate, str, str | None]:
    """Build the operator-chosen work item from live GitHub state.

    Operator force is the owner's explicit decision, so the factory's own
    pacing and selection rules (queue limits, review backlog, retry counts,
    cooldowns, intake authorization, dependency or blocked labels, CI still
    running) do not hide the item. It must be open, and it must not be held by
    another worker (:class:`TargetBusy`); the operator may stop or take over
    that worker explicitly.

    Returns the candidate, the action to run and the stage label to set.
    """
    target = target_json(number)
    if str(target.get('state') or '').lower() != 'open':
        raise RuntimeError(f'#{number} is closed. Reopen it to work on it.')
    is_pr = 'pull_request' in target
    if kind == 'pr' and not is_pr:
        raise RuntimeError(f'#{number} is an issue, not a pull request. Use issue:{number}.')
    if kind == 'issue' and is_pr:
        kind = 'pr'
    if kind == 'issue':
        linked = [
            pr for pr in list_prs()
            if linked_issue_from_pr(pr) == number and pr_suppresses_issue_candidate(pr, {})
        ]
        if len(linked) > 1:
            numbers = ', '.join(f"#{int(pr['number'])}" for pr in linked)
            raise RuntimeError(f'Issue #{number} has several open pull requests ({numbers}). Choose one with pr:N.')
        if linked:
            # Continue the existing pull request instead of starting a duplicate.
            return forced_target_candidate('pr', int(linked[0]['number']), requested_action)
    if is_manual_only(target.get('body')) or agent_hold(labels_of(target)):
        raise RuntimeError(
            f'#{number} is marked manual-only or held by {agent_hold(labels_of(target)) or "its description"}. '
            'Remove that marker or label to let a model work on it.'
        )
    labels = labels_of(target)
    owners = _owners(labels)
    if owners:
        raise TargetBusy(number, sorted(owners)[0])
    if kind == 'issue':
        candidate = Candidate('issue', number, provenance_lane(labels), 0,
                              str(target.get('created_at') or ''), stage=stage_of(labels))
        return candidate, 'implement', 'factory:building'
    live = live_pr_state(number)
    conflicted = (live.get('mergeable') is False
                  or str(live.get('mergeable_state') or '').upper() == 'DIRTY')
    stage = stage_of(labels)
    linked_issue = linked_issue_from_branch(str((live.get('head') or {}).get('ref') or ''))
    if linked_issue is not None:
        issue_labels = labels_of(target_json(linked_issue))
        if _owners(issue_labels):
            raise TargetBusy(linked_issue, sorted(_owners(issue_labels))[0])
    stage_actions = {
        'review': 'factory:review', 'review-findings': 'factory:changes-requested',
        'ci-repair': 'factory:ci', 'merge-conflict': 'factory:changes-requested',
    }
    if requested_action != 'advance' and requested_action in stage_actions:
        action = requested_action
        stage = stage_actions[requested_action]
    else:
        action = completion_action_for(stage, conflicted) or 'review-findings'
        if completion_action_for(stage, conflicted) is None:
            # No stage action (for example, ready or blocked): address any
            # outstanding feedback on the pull request.
            stage = 'factory:changes-requested'
    candidate = Candidate('pr', number, 3, 0, str(target.get('created_at') or ''),
                          linked_issue=linked_issue, stage=stage, conflicted=conflicted)
    return candidate, action, stage


def assign_forced_target(worker: str, target: tuple[str, int], requested_action: str = 'advance') -> Candidate | None:
    """Claim an operator-chosen item for one worker through the store ledger.

    The atomic store reservation still prevents two workers from owning the
    same item. Returns None when this worker could not be reserved.
    """
    candidate, action, stage = forced_target_candidate(target[0], target[1], requested_action)
    if worker_has_active_lease(worker):
        return None
    owner = f'factory:{worker}'
    numbers = [candidate.number]
    if candidate.kind == 'pr' and candidate.linked_issue is not None:
        numbers.insert(0, candidate.linked_issue)
    try:
        token = acquire_lease(REPO, worker, numbers, FIXED_LEASE_TTL_SECONDS, explicit=True)
    except Exception as exc:
        raise RuntimeError(f'Claim database unavailable ({type(exc).__name__}); nothing was started.') from None
    if token is None:
        return forced_refusal(REPO, worker, numbers)
    claimed: list[int] = []
    try:
        for number in numbers:
            current = labels_of(target_json(number))
            if _owners(current) - {owner}:
                raise TargetBusy(number, sorted(_owners(current) - {owner})[0])
            is_selected = number == candidate.number
            replace_factory_labels(number, owner, stage if is_selected else None)
            if not target_owned_by(number, owner):
                raise RuntimeError(f'Could not record the claim on #{number}.')
            claimed.append(number)
            head = None
            if is_selected and candidate.kind == 'pr':
                head = str((live_pr_state(number).get('head') or {}).get('sha') or '') or None
            record_controller_lease_activity(
                number, worker, candidate.kind if is_selected else 'issue',
                action=action if is_selected else None, source='operator', head=head,
            )
    except BaseException:
        for number in claimed:
            try:
                if target_owned_by(number, owner):
                    replace_factory_labels(number, 'factory:unowned')
            except Exception:
                pass
        try:
            release_lease(REPO, worker, token, 'operator-claim-rollback')
        except Exception as exc:
            print(f'[factory-controller] store claim rollback failed ({type(exc).__name__})', file=sys.stderr)
        raise
    print(f'[factory-controller] operator force: claimed {candidate.kind} #{candidate.number} '
          f'action={action} for Factory {worker}', file=sys.stderr)
    return candidate


def operator_forced_claim(number: int, worker: str) -> bool:
    """Return whether this worker's latest trusted claim on #number was operator-forced."""
    try:
        pages = gh_json(['api', '--paginate', '--slurp', f'repos/{REPO}/issues/{number}/comments?per_page=100'])
    except RuntimeError:
        return False
    pattern = re.compile(
        rf'{MARKER_PREFIX}-factory-controller-claim-v1:(?:issue|pr)-' + str(number) + ':' + re.escape(worker) + r':\d{10}(?P<fields>[^>]*)-->'
    )
    latest: str | None = None
    for comment in flatten_pages(pages):
        if not comment_is_trusted(comment):
            continue
        for match in pattern.finditer(str(comment.get('body') or '')):
            latest = match.group('fields')
    return latest is not None and ':source=operator' in latest


def dispatch_next(
    provider: str,
    model: str = 'auto',
    *,
    force_slot: bool = False,
    slot_index: int | None = None,
    target: str = '',
    action: str = 'advance',
) -> dict[str, object]:
    """Assign a provider worker to real work and start it.

    Without force, normal scheduling applies. With operator force the request
    always results in work when any exists: the factory's own pacing (worker
    health history, configured capacity, queue and review limits, cooldowns,
    budgets, pauses) never refuses it, and if every regular worker identity is
    busy an extra operator identity is used. Force never takes an item another
    worker holds (status ``busy``) and never switches provider. An ``exact:``
    model prefix makes the pin exact-or-nothing; otherwise an unavailable pinned
    model falls back inside the same provider at run time.
    """
    from factory_completion_controller import (
        capacity_report, durable_factory_state, load_manifest_candidates,
    )
    from factory_worker_slots import operator_slots, slots as registered_slots, sources

    source = 'opencode-free' if provider == 'opencode-go' else provider
    if source not in sources():
        raise ValueError(f'Unsupported factory provider: {provider}')
    from factory_manual_model import validate_model_syntax
    from factory_pinned_model import split_request
    model, exact = split_request(model)
    if model != 'auto':
        validate_model_syntax(source, model)
    if slot_index is not None and not force_slot:
        raise ValueError('An explicit slot requires --force-slot')
    if slot_index is not None and slot_index < 1:
        raise ValueError('Slot index must be positive')
    target_spec: tuple[str, int] | None = None
    if target:
        match = re.fullmatch(r'(issue|pr):([1-9][0-9]*)', target)
        if match is None:
            raise ValueError('Target must be issue:NUMBER or pr:NUMBER')
        target_spec = (match.group(1), int(match.group(2)))

    provider_slots = [
        entry for entry in registered_slots() if entry.source == source
        and (slot_index is None or entry.index + 1 == slot_index)
    ]
    if force_slot and slot_index is not None and not provider_slots:
        provider_slots = [
            entry for entry in operator_slots(source) if entry.index + 1 == slot_index
        ]
    if not provider_slots:
        raise ValueError('Requested slot does not exist for this provider')

    if force_slot:
        # Regular identities first, then operator overflow identities, so a
        # forced request is never refused because configured capacity is full.
        eligible = [entry.worker for entry in provider_slots]
        if slot_index is None:
            eligible += [entry.worker for entry in operator_slots(source)]
    else:
        _view, health = durable_factory_state()
        capacity = capacity_report(load_manifest_candidates(), health, now_epoch=int(time.time()))
        allowed = {str(row['worker']) for row in
                   cast(list[dict[str, str]], capacity['executable_candidates'])}
        eligible = [entry.worker for entry in provider_slots if entry.worker in allowed]

    skipped: list[str] = []
    busy_workers = 0
    for worker in eligible:
        if force_slot:
            reason = worker_skip_reason(worker, operator_force=True, named=True)
            if reason is not None:
                if 'already has an active lease' in reason:
                    busy_workers += 1
                    continue
                # Credentials are per provider, so every identity fails alike.
                return {
                    'status': 'deferred', 'provider': source, 'model': model,
                    'forced': True, 'slot_index': slot_index, 'blocker': 'credentials',
                    'reason': f'The {provider} provider is not usable: {reason}.',
                    'details': [reason],
                }
        try:
            candidate = (
                assign(worker, target=target_spec, requested_action=action, operator_force=True, named=True)
                if force_slot and target_spec
                else assign(worker, target=target_spec, named=True) if target_spec
                else assign(worker, operator_force=True, named=True) if force_slot
                else assign(worker, named=True)
            )
        except TargetBusy as busy:
            return {
                'status': 'busy', 'provider': source, 'model': model,
                'forced': force_slot, 'number': busy.number, 'owner': busy.owner,
                'owner_worker': busy.owner.removeprefix('factory:'),
                'reason': f'#{busy.number} is already being worked on by {busy.owner}.',
            }
        except RuntimeError as exc:
            if target_spec and (
                force_slot
                or 'No eligible work on this target' in str(exc)
                or 'blocked or still waiting on checks' in str(exc)
                or 'not currently executable' in str(exc).lower()
            ):
                skipped.append(str(exc))
                break
            raise
        if candidate is None:
            # Only for operator commands, provide a useful reason when a
            # registered slot cannot start. Do not mutate it or steal a lease.
            if force_slot:
                skipped.append(worker_skip_reason(worker, operator_force=True, named=True) or 'No executable unowned work')
                if not target_spec:
                    # The shared pool is the same for every identity.
                    break
            continue
        try:
            run_gh([
                'workflow', 'run', 'free-model-factory-entry.yml', '--repo', REPO,
                '--ref', 'main', '-f', f'worker={worker}',
                '-f', 'assignment=' + json.dumps(
                    assignment_context(candidate, worker) | ({'exact_model': True} if exact else {}) | ({'operator_forced': True} if force_slot and target_spec else {})),
                '-f', f'model={model}', '-f', f'provider={source}',
            ])
        except RuntimeError:
            # An accepted but timed-out workflow_dispatch may already exist.
            # Retaining the lease is safer than risking duplicate execution.
            factory_active_workers.forget(REPO)
            raise RuntimeError('Could not confirm worker dispatch. Inspect Actions before retrying.') from None
        factory_active_workers.note_dispatched(REPO, worker)
        if candidate.kind == 'issue':
            chosen_action: str | None = 'implement'
        elif force_slot and target_spec and action != 'advance':
            chosen_action = action
        elif force_slot:
            # Forced PR work with no stage action addresses review feedback.
            chosen_action = completion_action_for(candidate.stage, candidate.conflicted) or 'review-findings'
        else:
            chosen_action = completion_action_for(candidate.stage, candidate.conflicted)
        result: dict[str, object] = {
            'status': 'dispatched', 'kind': candidate.kind, 'number': candidate.number,
            'action': chosen_action, 'worker': worker, 'provider': source, 'model': model,
            'dispatched_at': int(time.time()),
        }
        if force_slot:
            result.update(forced=True, slot_index=slot_index)
        return result
    if not force_slot and not target_spec:
        return {
            'status': 'deferred', 'provider': source, 'model': model,
            'reason': ('No eligible provider slot currently has executable work. '
                       'Review stage, leases, source budgets, and capacity.'),
        }
    if force_slot and not skipped and busy_workers:
        reason = f'All {busy_workers} {provider} workers are running right now.'
    elif force_slot and not target_spec:
        reason = 'Nothing is waiting: every open item is finished, claimed or blocked.'
    elif force_slot:
        reason = skipped[0] if skipped else 'The selected item could not be claimed.'
    else:
        reason = ('No registered slot passed historical health screening'
                  if not eligible else
                  'All checked slots are currently busy, blocked or have no executable work')
    return {
        'status': 'deferred', 'provider': source, 'model': model,
        'forced': force_slot, 'slot_index': slot_index,
        'eligible_slots': len(eligible),
        'reason': reason,
        'details': list(dict.fromkeys(skipped))[:5],
    }


def dispatch_target(kind: str, number: int, action: str, model: str = 'auto') -> dict[str, object]:
    """Allocate one worker to the selected target through the normal lease policy.

    Targets that are currently ineligible or waiting on checks are deferred, not
    treated as fatal dispatcher errors. This keeps CI-aware/manual dispatch from
    crashing when the selected PR is simply not actionable yet.
    """
    from factory_completion_controller import (
        capacity_report,
        durable_factory_state,
        load_manifest_candidates,
    )

    if number <= 0:
        raise ValueError('Select a positive issue or PR number.')
    if model not in ('auto', 'opencode-go/hy4-flash-free'):
        raise ValueError('Unsupported manual model choice')
    _view, health = durable_factory_state()
    capacity = capacity_report(load_manifest_candidates(), health, now_epoch=int(time.time()))
    workers = cast(list[dict[str, str]], capacity['executable_candidates'])
    if model != 'auto':
        # Operator pin selects the existing Go lane, not a second source or
        # a guessed model from an unrelated provider's worker. Capacity and
        # assignment still use the same authoritative lease policy.
        workers = [
            row for row in workers
            if worker_sources().get(row['worker']) == 'opencode-free'
        ]
    preferred_role = 'starter' if kind == 'issue' else (
        'finisher' if action in {'review-findings', 'ci-repair'} else ''
    )
    from factory_work_policy import worker_delivery_role

    last_defer_reason: str | None = None
    for worker in sorted(
        {row['worker'] for row in workers},
        key=lambda worker: (worker_delivery_role(worker) != preferred_role, int(worker)),
    ):
        try:
            candidate = assign(worker, target=(kind, number), requested_action=action)
        except RuntimeError as exc:
            reason = str(exc)
            if (
                'blocked or still waiting on checks' in reason
                or 'No eligible work on this target' in reason
                or 'Requested action is unavailable' in reason
                or 'not currently executable' in reason.lower()
            ):
                last_defer_reason = reason
                break
            raise
        if candidate is None:
            continue
        selected = (
            'implement' if candidate.kind == 'issue'
            else completion_action_for(candidate.stage, candidate.conflicted)
        )
        try:
            dispatch_args = [
                'workflow', 'run', 'free-model-factory-entry.yml', '--repo', REPO,
                '--ref', 'main', '-f', f'worker={worker}',
                '-f', 'assignment=' + json.dumps(assignment_context(candidate, worker)),
            ]
            if model != 'auto':
                dispatch_args += ['-f', f'model={model}']
            run_gh(dispatch_args)
        except RuntimeError:
            # A timed-out POST can already have queued the worker. Keep its lease
            # so another manual request cannot start duplicate work.
            factory_active_workers.forget(REPO)
            raise RuntimeError('Could not confirm worker dispatch. Check Actions before retrying.') from None
        factory_active_workers.note_dispatched(REPO, worker)
        return {
            'kind': candidate.kind,
            'number': candidate.number,
            'action': selected if action == 'advance' else action,
            'status': 'dispatched',
            'worker': worker,
            'model': model,
        }
    if last_defer_reason is not None:
        print(f'[factory-controller] deferring {kind}:{number}: {last_defer_reason}', file=sys.stderr)
        return {
            'kind': kind,
            'number': number,
            'action': action,
            'status': 'deferred',
            'reason': last_defer_reason,
        }
    print(
        f'[factory-controller] deferring {kind}:{number}: no eligible action or available independent worker',
        file=sys.stderr,
    )
    return {
        'kind': kind,
        'number': number,
        'action': action,
        'status': 'deferred',
        'reason': 'No eligible action or available independent worker. Check current stage, leases, and factory capacity.',
    }


def main() -> int:
    """Run the factory controller command-line interface."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('reconcile')
    subparsers.add_parser('capacity')
    stale_parser = subparsers.add_parser('stale')
    stale_parser.add_argument('--dry-run', action='store_true', help='Report without taking action')
    assign_parser = subparsers.add_parser('assign')
    assign_parser.add_argument('--worker', required=True)
    assign_parser.add_argument('--kinds', nargs='*', default=None)
    assign_parser.add_argument('--batch-reconciled', action='store_true',
        help='Global reconciliation already succeeded once in the trusted dispatch batch')
    next_parser = subparsers.add_parser('dispatch-next')
    next_parser.add_argument('--provider', required=True)
    next_parser.add_argument('--model', default='auto')
    next_parser.add_argument('--force-slot', action='store_true')
    next_parser.add_argument('--slot-index', type=int)
    next_parser.add_argument('--target', default='')
    next_parser.add_argument('--action', default='advance', choices=(
        'advance', 'implement', 'merge-conflict', 'ci-repair', 'review-findings', 'review',
    ))
    target_parser = subparsers.add_parser('dispatch-target')
    target_parser.add_argument('--kind', required=True, choices=('issue', 'pr'))
    target_parser.add_argument('--number', required=True, type=int)
    target_parser.add_argument('--action', required=True, choices=(
        'advance', 'implement', 'merge-conflict', 'ci-repair', 'review-findings', 'review',
    ))
    target_parser.add_argument('--model', choices=('auto', 'opencode-go/hy4-flash-free'),
                               default='auto')
    validate_parser = subparsers.add_parser('validate-assignment')
    validate_parser.add_argument('--worker', required=True)
    inspect_parser = subparsers.add_parser('inspect')
    inspect_parser.add_argument('--worker', required=True)
    release_parser = subparsers.add_parser('release')
    release_parser.add_argument('--worker', required=True)
    release_parser.add_argument('--reason', default='controller-release')
    release_parser.add_argument('--claim-id', default='')
    recovery_parser = subparsers.add_parser('conflict-recovery')
    recovery_parser.add_argument('--worker', required=True)
    explain_parser = subparsers.add_parser('explain')
    explain_parser.add_argument('--issue', required=True, type=int)
    explain_parser.add_argument('--json', action='store_true', help='Emit JSON instead of text')
    subparsers.add_parser('snapshot')
    ready_parser = subparsers.add_parser('ready', help='the ranked pool the next pass walks')
    ready_parser.add_argument('--limit', type=int, default=factory_ready.PUBLISHED_LIMIT)
    ready_parser.add_argument('--publish', action='store_true', help='store the top items for the status page')
    subparsers.add_parser('signal-completion')
    subparsers.add_parser('signal-roster')
    args = parser.parse_args()
    if args.command == 'snapshot':
        print(json.dumps({'issues': list_issues(), 'prs': list_prs()}))
        return 0
    if args.command == 'reconcile':
        released = reconcile_stale_leases()
        repaired = reconcile_contradictory_labels()
        try:
            links = factory_deps.reconcile(REPO, list_issues()) if ledger_enabled() else {}
        except Exception as exc:
            links = {'error': type(exc).__name__}
        print(json.dumps({'released': released, 'contradictions_repaired': repaired, 'dependencies': links}))
        return 0
    if args.command == 'ready':
        print(json.dumps(factory_ready.report(REPO, ready_inputs, args.limit, args.publish and ledger_enabled())))
        return 0
    if args.command == 'capacity':
        print(json.dumps(omniroute_free_entry_capacity()))
        return 0
    if args.command == 'stale':
        print(json.dumps(check_stale_prs(dry_run=args.dry_run)))
        return 0
    if args.command == 'assign':
        if not is_authorized_fixed_worker(args.worker):
            raise SystemExit(f'unsupported fixed-model worker: {args.worker}')
        skip_reason = worker_skip_reason(args.worker)
        if skip_reason is not None:
            print(f'[factory-controller] {skip_reason}; skipping dispatch', file=sys.stderr)
            # A busy/paused/capped worker says nothing about the shared pool.
            print(json.dumps({'kind': 'none', 'reason': 'worker_unavailable', 'detail': skip_reason}))
            return 0
        kinds = tuple(args.kinds) if args.kinds else None
        candidate = assign_from_pool(args.worker, kinds=kinds, batch_reconciled=args.batch_reconciled)
        if candidate is None:
            print(json.dumps({'kind': 'none', 'reason': 'no_work', 'detail': trace.summary()}))
            return 0
        print(json.dumps(assignment_context(candidate, args.worker)))
        return 0
    if args.command == 'dispatch-next':
        print(json.dumps(dispatch_next(
            args.provider, args.model, force_slot=args.force_slot,
            slot_index=args.slot_index, target=args.target, action=args.action,
        )))
        return 0
    if args.command == 'dispatch-target':
        print(json.dumps(dispatch_target(args.kind, args.number, args.action, args.model)))
        return 0
    if args.command == 'validate-assignment':
        print(json.dumps(validate_assignment(args.worker, sys.stdin.read())))
        return 0
    if args.command == 'inspect':
        print(json.dumps(inspect_assignment(args.worker)))
        return 0
    if args.command == 'conflict-recovery':
        result = selectivity_conflict_recovery(args.worker)
        print(json.dumps(result))
        return 0
    if args.command == 'explain':
        report = explain_issue(args.issue)
        print(json.dumps(report, sort_keys=True) if args.json else render_explain_report(report))
        return 0
    if args.command == 'signal-completion':
        print(json.dumps(signal_completion_mode(), sort_keys=True))
        return 0
    if args.command == 'signal-roster':
        print(json.dumps(signal_roster_mode(), sort_keys=True))
        return 0
    outcome = {}
    released = release_worker(args.worker, reason=args.reason, details=outcome, expected_claim=args.claim_id)
    print(json.dumps(outcome if outcome else {'released': released}))
    # A live, actionable claim must never make a cleanup job green.
    # labels_pending: the store claim is discharged but GitHub labels still need
    # stale-lease reconciliation; keep the job red so the gap stays visible.
    return 1 if outcome.get('status') in ('failed', 'deferred', 'missing_claim', 'labels_pending') else 0

if __name__ == "__main__":
    raise SystemExit(factory_crash.run(main))
