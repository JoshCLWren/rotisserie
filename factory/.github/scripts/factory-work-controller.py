#!/usr/bin/env python3
"""Deterministic GitHub assignment and lease reconciliation for ComicPile."""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
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
from factory_work_policy import (BLOCKED_LABELS, FACTORY_NO_DIFF_RETRY_LIMIT, FACTORY_NO_DIFF_RETRY_RESET_SECONDS, FACTORY_PR_WIP_LIMIT, FACTORY_REVIEW_BACKLOG_LIMIT, FIXED_LEASE_TTL_SECONDS, LOCAL_LEASE_TTL_SECONDS, FIXED_OWNER_RE, NoDiffAttempt, NON_EXECUTABLE_ISSUES, OWNER_RE, REQUIRED_CHECK_FAILURE_STATES, STAGE_LABELS, STAGE_PRECEDENCE, Candidate, build_candidates, comment_is_trusted, eligibility, env_positive_int, factory_pr_wip_count, factory_review_backlog_count, is_manual_only, item_is_unowned, issue_bypasses_wip_limit, issue_explicitly_closed_by_pr, labels_of, lease_is_stale, linked_issue_from_branch, linked_issue_from_pr, order_candidates_for_worker, owner_of, parse_no_diff_attempts_from_comments, plan_distinct_assignments, pr_suppresses_issue_candidate, pr_cutover_allowed, stage_of)
from factory_jev_shadow import apply_completion_ranking, apply_jev_ranking
from stale_pr_decay import StalePRGuard, list_factory_prs
from factory_review_policy import recorded_pr_contributors, trusted_comment_bodies
from factory_quota_pause import describe as describe_quota_pause, is_paused as quota_pause_for
from factory_source_budget import budget_block_reason, worker_sources
from factory_attempt_history import read_comment_history
from factory_discovery import canonical_target, discover, update_labels
from factory_state_ledger import acquire_lease, release_lease, renew_verified_workers, worker_claim
REPO = os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
GH_TIMEOUT_SECONDS = env_positive_int("FACTORY_GH_TIMEOUT_SECONDS", 120)
ASSIGNMENT_WRITER_WORKFLOW_PATH = ".github/workflows/fixed-model-factory-dispatch.yml"
ROTISSERIE_CONTROL_PATH = Path(".github/factory-rotisserie-control.json")
STRIKE_RESET_RE = re.compile(
    r"comic-pile-factory-strike-reset-v1:issue-(?P<issue>\d+):pr-(?P<pr>\d+):"
    r"excluded-producer-(?P<worker>\d+|unknown)"
)
IMPLEMENT_CLAIM_RE = re.compile(
    r"comic-pile-factory-implement-claim-v3:issue-(?P<issue>\d+):"
    r"opencode-(?:free-model|nvidia|omniroute)-factory-(?P<worker>\d+):"
)
LEASE_ACTIVITY_PATTERNS = (
    re.compile(r"comic-pile-factory-implement-(?:claim|progress)-v3:issue-\d+:[^:>]+:(\d{10})"),
    re.compile(r"comic-pile-factory-fix-(?:claim|progress)-v3:[^:>]+:[^:>]+:(\d{10})"),
    re.compile(r"comic-pile-factory-review-claim-v2:[^:>]+:[^:>]+:(\d{10})"),
    re.compile(r"comic-pile-factory-controller-claim-v1:(?:issue|pr)-\d+:\d+:(\d{10})"),
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
    command = ['gh', *args]
    try:
        proc = subprocess.run(command, input=None if input_json is None else json.dumps(input_json), text=True, capture_output=True, check=False, timeout=GH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(command)} timed out") from exc
    if check and proc.returncode:
        raise RuntimeError(f"{' '.join(command)} failed ({proc.returncode}): {proc.stderr.strip()}")
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
        # Target-scoped snapshots must retain the live Rust cutover gate.
        # Without the epic, build_candidates mistakenly interprets the narrow
        # candidate view as proof #4335 is closed and admits deferred work.
        epic_number = eligibility.RUST_CUTOVER_EPIC
        if not any(int(row['number']) == epic_number for row in rows):
            epic = target_json(epic_number)
            if str(epic.get('state') or '').lower() == 'open':
                rows.append(canonical_target('issue', epic))
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
    return discover(REPO, 'pr', lambda: cast(list[dict[str, Any]], gh_json([
        'pr', 'list', '--repo', REPO, '--state', 'open', '--limit', '500',
        '--json', 'number,title,body,labels,headRefName,headRefOid,createdAt,updatedAt,isDraft,mergeable,mergeStateStatus,isCrossRepository',
    ])))


def rotisserie_issue_intake(issues: list[dict[str, Any]], prs: list[dict[str, Any]]) -> set[int] | None:
    """Return authorized issue numbers, or None while the lane is legacy."""
    try:
        control = json.loads(ROTISSERIE_CONTROL_PATH.read_text(encoding="utf-8"))
        stage = control.get("stage")
        if stage == "legacy":
            return None
        if stage not in {"canary", "expanded"}:
            raise ValueError("unsupported adopter stage")
        if control.get("lane") != {
            "name": "issue-intake",
            "subjects": ["label:factory:unowned"],
        }:
            raise ValueError("adopter control is outside the issue-intake lane")

        from factory_rotisserie_adopter import (
            FACTORY_PR_WIP_LIMIT,
            FACTORY_REVIEW_BACKLOG_LIMIT,
            factory_pr_wip_count,
            factory_review_backlog_count,
            graph_snapshot,
        )

        revision = os.environ.get("GITHUB_SHA", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip().lower()
        now = int(time.time())
        open_numbers = {
            int(issue["number"]) for issue in issues
            if str(issue.get("state") or "OPEN").upper() == "OPEN"
        }
        issue_map = {int(issue["number"]): issue for issue in issues}
        cutover_open = eligibility.RUST_CUTOVER_EPIC in open_numbers
        eligible_prs = [
            pr for pr in prs
            if pr_cutover_allowed(pr, issue_map, cutover_open=cutover_open)
        ]
        snapshot = graph_snapshot(
            {
                "schema_version": 1,
                "repository": REPO,
                "revision": revision,
                "captured_at": now,
                "issues": issues,
                "pull_requests": eligible_prs,
            }
        )
        with tempfile.TemporaryDirectory(prefix="comic-pile-rotisserie-intake-") as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            config_path = root / "operator.toml"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            config_path.write_text(
                'schema_version = 1\n[repository]\nhost = "github.com"\n'
                'owner = "JoshCLWren"\nname = "comic-pile"\n[repositories]\n'
                'allow = [{ host = "github.com", owner = "JoshCLWren", name = "comic-pile" }]\n'
                '[local]\nsnapshot = "snapshot.json"\nstate_directory = "state"\n'
                'mutations_enabled = false\n',
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    os.environ.get("FACTORY_ROTISSERIE_COMMAND", "rotisserie"),
                    "--config", str(config_path), "decide", "--snapshot", str(snapshot_path),
                    "--revision", revision, "--at", str(now),
                    "--completion-backlog", str(factory_review_backlog_count(eligible_prs)),
                    "--backlog-limit", str(FACTORY_REVIEW_BACKLOG_LIMIT),
                    "--active-changes", str(factory_pr_wip_count(eligible_prs)),
                    "--wip-limit", str(FACTORY_PR_WIP_LIMIT),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        envelope = json.loads(completed.stdout)
        if envelope.get("status") != "ok":
            raise ValueError("Rotisserie decision was not successful")
        observations = envelope["evidence"]["decisions"]["observations"]
        return {
            int(item["subject"].removeprefix("work:"))
            for item in observations
            if item.get("dimension") == "eligibility"
            and item.get("outcome") == "eligible"
            and str(item.get("subject", "")).startswith("work:")
        }
    except (KeyError, OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"[factory-controller] Rotisserie issue intake unavailable; holding fresh issues: {error}", file=sys.stderr)
        return set()


def target_json(number: int) -> dict[str, Any]:
    """Fetch one issue-compatible GitHub target payload."""
    return cast(dict[str, Any], gh_json(['api', f'repos/{REPO}/issues/{number}']))


def live_pr_state(number: int) -> dict[str, Any]:
    """Read the authoritative PR head, merge state, and labels at lease time."""
    return cast(dict[str, Any], gh_json(['api', f'repos/{REPO}/pulls/{number}']))


def completion_action_for(stage: str | None, conflicted: bool) -> str | None:
    """Derive the same completion action the dispatcher offered for this stage."""
    if conflicted:
        return 'merge-conflict'
    return {
        'factory:ci': 'ci-repair',
        'factory:changes-requested': 'review-findings',
        'factory:review': 'review',
    }.get(stage or '')


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
        if related_owner_target is not None:
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
    if is_manual_only(target.get("body")):
        print(
            f"[factory-controller] skip factory label write on #{number}: manual-only",
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
    labels = [label for label in current if not OWNER_RE.fullmatch(label) and label not in STAGE_LABELS and (label != 'factory')]
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

    Ranking reads can use a bounded Neon snapshot. Actual issue assignment
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

    Treat missing, unparseable, and unknown evidence as waiting. Never turn a
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


def candidate_is_live_executable(candidate: Candidate) -> bool:
    """Return whether a ranked candidate can use a model *now*.

    CI-in-progress suppresses every PR stage, including review, repair and
    conflict work. CI-stage success is reconciled by the trusted controller
    instead of assigning a model just to notice that CI finished.
    """
    if candidate.kind == 'issue':
        return not issue_has_open_blocker(candidate.number)
    target = target_json(candidate.number)
    labels = {label['name'] for label in target.get('labels', [])}
    if 'factory:ready' in labels:
        return False
    outcome = required_checks_outcome(candidate.number)
    if outcome == 'waiting':
        return False
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
    marker = f'<!-- comic-pile-factory-controller-claim-v1:{kind}-{number}:{worker}:{epoch}'
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
        return False
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
            return False
    # The ranked snapshot may have been taken before a CI run started. Do not
    # allocate a Neon lease to a PR that is currently waiting on CI.
    if candidate.kind == 'pr' and candidate.stage in STAGE_LABELS:
        if not candidate_is_live_executable(candidate):
            return False
    if candidate.kind == 'issue' and issue_has_open_blocker(candidate.number, live=True):
        # A fresh dependency was added after ranking; never convert an
        # advisory Neon cache hit into a blocked GitHub lease mutation.
        return False

    if worker_has_active_lease(worker):
        return False

    # Acquire one atomic worker + issue/PR reservation before mutating GitHub.
    # GitHub live checks remain required; a Neon conflict never grants a claim.
    try:
        claim_token = acquire_lease(REPO, worker, numbers, FIXED_LEASE_TTL_SECONDS)
    except Exception as exc:
        print(f'[factory-controller] Neon lease unavailable; refusing duplicate-risk assignment ({type(exc).__name__})', file=sys.stderr)
        return False
    if claim_token is None:
        return False

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
            # A failed GitHub write must not strand the Neon claim.
            try:
                release_lease(REPO, worker, claim_token, 'claim-rollback')
            except Exception as exc:
                print(f'[factory-controller] Neon claim rollback failed ({type(exc).__name__})', file=sys.stderr)
    claimed: list[int] = []
    try:
        # Close the race between the first check and the Neon reservation.
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
        # Round *down* to share the same Neon GET key across near-simultaneous
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
    return read_comment_history(
        now_epoch=now_epoch, reset_seconds=FACTORY_NO_DIFF_RETRY_RESET_SECONDS,
        scope=f'{REPO}:pr:{target_pr}' if target_pr is not None else REPO,
        cache_path=Path(cache) if cache else None, fetch_pages=fetch_pages,
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


class ActiveWorkerResult(tuple[set[int], set[str]]):
    """Pair of resolved workers and credible unresolved runs with set compatibility."""

    def __new__(cls, workers: set[int], unresolved: set[str]) -> ActiveWorkerResult:
        return tuple.__new__(cls, (workers, unresolved))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, set):
            return self[0] == other
        return tuple.__eq__(self, other)


def github_timestamp_epoch(value: object) -> int | None:
    """Parse one GitHub timestamp into epoch seconds when possible."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
    except ValueError:
        return None


def unresolved_run_has_credible_liveness(run: dict[str, Any], *, now_epoch: int) -> bool:
    """Return whether an unresolved active-status run still merits a safety fence."""
    status = str(run.get('status') or '').lower()
    if status not in ('queued', 'in_progress'):
        return True
    run_activity = [
        epoch
        for key in ('created_at', 'updated_at')
        if (epoch := github_timestamp_epoch(run.get(key))) is not None
    ]
    if not run_activity:
        return True
    if now_epoch - max(run_activity) <= FIXED_LEASE_TTL_SECONDS:
        return True
    run_id = str(run.get('id') or '')
    if not run_id.isdigit():
        return True
    try:
        pages = gh_json(['api', '--paginate', '--slurp', f'repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100'])
    except RuntimeError:
        return True
    page_values = pages if isinstance(pages, list) else []
    jobs: list[dict[str, Any]] = []
    saw_jobs_payload = False
    for page in page_values:
        if not isinstance(page, dict):
            continue
        page_jobs = page.get('jobs')
        if not isinstance(page_jobs, list):
            continue
        saw_jobs_payload = True
        jobs.extend(job for job in page_jobs if isinstance(job, dict))
    if not saw_jobs_payload:
        return True
    if not jobs:
        return False
    for job in jobs:
        job_status = str(job.get('status') or '').lower()
        if job_status in ('queued', 'in_progress'):
            return True
        job_activity = [
            epoch
            for key in ('started_at', 'completed_at')
            if (epoch := github_timestamp_epoch(job.get(key))) is not None
        ]
        if not job_activity:
            return True
        if now_epoch - max(job_activity) <= FIXED_LEASE_TTL_SECONDS:
            return True
    return False


def active_fixed_workers() -> ActiveWorkerResult:
    """Return resolved workers and credible unresolved active-status runs."""
    workers: set[int] = set()
    runs: list[dict[str, Any]] = []
    unresolved_run_ids: set[str] = set()
    unresolved_runs: dict[str, dict[str, Any]] = {}
    for workflow in ('free-model-factory-entry.yml', 'factory-fork-execution.yml'):
        for status in ('queued', 'in_progress'):
            try:
                pages = gh_json(['api', '--paginate', '--slurp', f'repos/{REPO}/actions/workflows/{workflow}/runs?status={status}&per_page=100'])
            except RuntimeError:
                prefix = '' if workflow == 'free-model-factory-entry.yml' else 'fork-'
                unresolved_run_ids.add(f'{prefix}{status}-run-query-unavailable')
                continue
            page_values = pages if isinstance(pages, list) else []
            for page in page_values:
                if isinstance(page, dict):
                    runs.extend(cast(list[dict[str, Any]], page.get('workflow_runs') or []))
    for index, run in enumerate(runs):
        title = str(run.get('display_title') or run.get('name') or '')
        match = re.search(r'\bFactory\s+(\d+)\b', title)
        if match:
            workers.add(int(match.group(1)))
            continue
        run_id = str(run.get('id') or '')
        unresolved_id = run_id or f'missing-run-id-{index}'
        unresolved_run_ids.add(unresolved_id)
        if run_id:
            unresolved_runs[run_id] = run
    numeric_unresolved = {value for value in unresolved_run_ids if value.isdigit()}
    if numeric_unresolved:
        try:
            from factory_runtime_view import run_workers
            matches = run_workers(REPO, sorted(numeric_unresolved))
            for run_id, worker in matches.items():
                if run_id in numeric_unresolved and worker.isdecimal() and is_authorized_fixed_worker(worker):
                    workers.add(int(worker))
                    unresolved_run_ids.discard(run_id)
        except Exception as exc:
            print(f'[factory-controller] run-to-worker evidence unavailable ({type(exc).__name__}); preserving unresolved run fence', file=sys.stderr)
    now_epoch = int(time.time())
    for run_id, run in unresolved_runs.items():
        if run_id not in unresolved_run_ids:
            continue
        if unresolved_run_has_credible_liveness(run, now_epoch=now_epoch):
            continue
        unresolved_run_ids.discard(run_id)
        print(f'[factory-controller] ignoring expired unresolved active-status run {run_id}: no credible worker execution evidence', file=sys.stderr)
    return ActiveWorkerResult(workers, unresolved_run_ids)


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
        if is_manual_only(item.get("body")):
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
        replace_factory_labels(number, norm_owner, norm_stage)
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
    # The current Actions run roster is already verified. Renew only the
    # existing reservations of workers still demonstrably executing.
    try:
        renew_verified_workers(REPO, [str(worker) for worker in active])
    except Exception as exc:
        print(f'[factory-controller] verified Neon renewal deferred ({type(exc).__name__})', file=sys.stderr)
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
        if is_manual_only(targets[number].get('body')):
            continue
        fixed_owner = FIXED_OWNER_RE.fullmatch(owner)
        if fixed_owner and (unresolved_runs or int(fixed_owner.group("worker")) in active):
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
        paired_number = pairs.get(number)
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
        if not replace_factory_labels(
            number,
            'factory:unowned',
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
            # label. A newer Neon claim never matches this captured token.
            if all(
                not target_owned_by(number, f'factory:{worker_id}')
                for number in claim_numbers
            ):
                release_lease(REPO, worker_id, token, 'stale-lease-reconciled')
        except Exception as exc:
            print(f'[factory-controller] stale Neon release deferred ({type(exc).__name__})', file=sys.stderr)
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
    issue_map = {int(issue['number']): issue for issue in issues}
    cutover_open = eligibility.RUST_CUTOVER_EPIC in issue_map
    eligible_prs = [
        pr for pr in prs
        if pr_cutover_allowed(pr, issue_map, cutover_open=cutover_open)
    ]
    from factory_work_policy import factory_review_backlog_count
    backlog = factory_review_backlog_count(eligible_prs)
    snapshot = omniroute_free_entry_capacity()
    from factory_capacity_policy import completion_worker_target, FleetDemand
    demand = FleetDemand(
        completion=sum(
            1 for pr in eligible_prs
            if pr.get('state') == 'OPEN' and not pr.get('isDraft')
        ),
        production=sum(
            1 for issue in issues
            if eligibility.rust_cutover_work_allowed(
                labels_of(issue), str(issue.get('body') or ''),
                cutover_open=cutover_open,
            )
        ),
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
        from factory_state_operations import enabled as neon_enabled, filter_workers
        if neon_enabled():
            allowed, blocked = filter_workers(REPO, [worker])
            return None if allowed else "; ".join(blocked)
    except Exception as exc:
        print(f"[factory-controller] Neon health unavailable: {type(exc).__name__}", file=sys.stderr)
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


def worker_skip_reason(worker: str) -> str | None:
    """Return why this worker itself cannot take work now, or None.

    These checks are about the worker (pause, credentials, lease, budget,
    entry capacity), not the shared work pool. Callers must not treat a
    non-None reason as evidence that there is no executable work.
    """
    quota_pause = quota_pause_for(worker)
    if quota_pause is not None:
        return describe_quota_pause(quota_pause)
    runtime_pause = runtime_quota_block_reason(worker)
    if runtime_pause is not None:
        return runtime_pause
    try:
        sources = worker_sources()
        source = sources.get(str(worker), "")
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
    skip_reason = worker_skip_reason(worker)
    if skip_reason is not None:
        print(f'[factory-controller] {skip_reason}; skipping dispatch', file=sys.stderr)
        return None
    return assign_from_pool(worker, kinds, target=target, requested_action=requested_action,
                            batch_reconciled=batch_reconciled)


def assign_from_pool(worker: str, kinds: tuple[str, ...] | None = None,
    *,
    target: tuple[str, int] | None = None,
    requested_action: str = 'advance',
    batch_reconciled: bool = False,
) -> Candidate | None:
    """Rank the shared pool for a worker already proven eligible."""
    # Trusted dispatcher batches run these two full-repository reconciliations
    # once before iterating workers. Standalone/manual assignment still checks
    # them here. Every actual claim remains live-verified in assign_candidate.
    if target is None and not batch_reconciled:
        reconcile_stale_leases()
        reconcile_contradictory_labels()
    issues = list_issues()
    prs = list_prs()
    # Resolve an issue wakeup to its canonical open PR *before* loading retry
    # history or asking Rotisserie to rank fresh issues. Otherwise every
    # worker candidate reads the entire retry window and authorizes issue
    # intake even though only the existing PR can be advanced.
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
        # Targeted PR wakeups require only that PR's retry evidence.
        # A full roster pass still needs the whole issue/PR retry window.
        attempt_records: list[NoDiffAttempt] | None = (
            load_no_diff_attempt_records(target_pr=target[1])
            if target is not None and target[0] == 'pr'
            else load_no_diff_attempt_records()
        )
    except RuntimeError as exc:
        attempt_records = None
        print(f'[factory-controller] no-diff retry history unavailable; holding fresh issue intake: {exc}', file=sys.stderr)
    issue_retry_counts: dict[int, int] = {}
    if attempt_records is not None:
        for attempt in attempt_records:
            if attempt.kind != 'issue':
                continue
            issue_retry_counts[attempt.number] = issue_retry_counts.get(attempt.number, 0) + 1
    candidates = build_candidates(
        issues,
        prs,
        no_diff_attempts_by_issue=issue_retry_counts,
        no_diff_attempt_records=attempt_records,
    )
    # A PR-only target never leases new issue work; issue-intake authorization
    # is unnecessary and can fail closed or consume remote calls on every
    # targeted repair/review wakeup.
    if target is None or target[0] == 'issue':
        authorized_issues = rotisserie_issue_intake(issues, prs)
        if authorized_issues is not None:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.kind != "issue" or candidate.number in authorized_issues
            ]
    if kinds is not None:
        candidates = [candidate for candidate in candidates if candidate.kind in kinds]
    elif attempt_records is None:
        candidates = [candidate for candidate in candidates if candidate.kind == 'pr']
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
    # Advisory retry cooldown is scoped to exact PR head AND stage.
    # Manual targets can override retry scheduling, never live merge/CI gates.
    if target is None:
        try:
            from factory_state_operations import cooling_prs
            pr_heads = {int(pr["number"]): str(pr.get("headRefOid") or "") for pr in prs}
            lookup = {c.number: (pr_heads.get(c.number, ""), c.stage or "factory:review")
                      for c in candidates if c.kind == "pr"}
            cooling = cooling_prs(REPO, lookup)
            if cooling:
                print(f"[factory-controller] exact-head retry cooling: {sorted(cooling)}", file=sys.stderr)
                candidates = [c for c in candidates if c.number not in cooling]
        except Exception as exc:
            print(f"[factory-controller] Neon retry read deferred ({type(exc).__name__})", file=sys.stderr)
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
            continue
        if (
            candidate.kind == "issue"
            and issue_excludes_worker_on_strike_retry(candidate.number, worker)
        ):
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
                    ):
                        return candidate
                elif assign_candidate(candidate, worker, expected_head=head, source='manual'):
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
        if assigned:
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


def assignment_context(candidate: Candidate, worker: str) -> dict[str, object]:
    """Export a small, fenced assignment after a successful dispatcher claim."""
    claim = worker_claim(REPO, worker, active_only=True)
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
    claim = worker_claim(REPO, worker, active_only=True)
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
        candidate = Candidate('pr', number, 3, 0, '', stage=str(context.get('stage') or ''))
        if not candidate_is_live_executable(candidate):
            raise ValueError('Assignment PR is waiting or no longer executable')
    return context


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
        f'<!-- comic-pile-factory-claim-released-v3:'
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
    """Release an exiting worker's exact Neon claim, never a later assignment.

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
        print(f'[factory-controller] Neon release snapshot unavailable ({type(exc).__name__})', file=sys.stderr)
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
                    # authoritative GitHub, not Neon discovery
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
                # has exited, so discharge its exact Neon claim anyway rather
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
        if replace_factory_labels(number, 'factory:unowned') is False:
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
    pr_rows = list_prs() if prs is None else list(prs)
    payload = issue
    if payload is None:
        payload = next((row for row in issue_rows if int(row['number']) == number), None)
    if payload is None:
        payload = target_json(number)
        issue_rows = [*issue_rows, payload]
    issue_map = {int(row['number']): row for row in issue_rows}
    suppressing = {
        linked
        for pr in pr_rows
        if (linked := linked_issue_from_pr(pr)) is not None
        and pr_suppresses_issue_candidate(pr, issue_map)
    }
    non_executable = set(NON_EXECUTABLE_ISSUES)
    intake = rotisserie_issue_intake(issue_rows, pr_rows)
    rotisserie_unauthorized: set[int] = set()
    if intake is not None:
        rotisserie_unauthorized = {int(row['number']) for row in issue_rows} - intake
        non_executable |= rotisserie_unauthorized
    counts = (
        load_no_diff_attempts() if no_diff_attempts_by_issue is None else no_diff_attempts_by_issue
    )
    verdict = eligibility.explain_issue_eligibility(
        number=number,
        state=str(payload.get('state') or 'OPEN'),
        labels=labels_of(payload),
        body=str(payload.get('body') or ''),
        open_numbers={int(row['number']) for row in issue_rows},
        suppressing_issue_numbers=suppressing,
        non_executable_numbers=non_executable,
        no_diff_attempts=counts.get(number, 0),
        no_diff_limit=FACTORY_NO_DIFF_RETRY_LIMIT,
    )
    owning_prs = sorted(
        int(pr['number'])
        for pr in pr_rows
        if linked_issue_from_pr(pr) == number and pr_suppresses_issue_candidate(pr, issue_map)
    )
    cutover_open = eligibility.RUST_CUTOVER_EPIC in {
        int(row['number']) for row in issue_rows
        if str(row.get('state') or 'OPEN').upper() == 'OPEN'
    }
    active_pr_rows = [
        pr for pr in pr_rows
        if pr_cutover_allowed(pr, issue_map, cutover_open=cutover_open)
    ]
    pr_wip = factory_pr_wip_count(active_pr_rows)
    review_backlog = factory_review_backlog_count(active_pr_rows)
    gates: dict[str, Any] = {
        'factory_pr_wip': pr_wip,
        'factory_pr_wip_limit': FACTORY_PR_WIP_LIMIT,
        'review_backlog': review_backlog,
        'review_backlog_limit': FACTORY_REVIEW_BACKLOG_LIMIT,
        'bypasses_wip_limit': issue_bypasses_wip_limit(payload),
        'bypasses_saturated_review_backlog': issue_bypasses_wip_limit(
            payload, review_backlog_saturated=True
        ),
        'rotisserie_intake': (
            'legacy-unrestricted'
            if intake is None
            else ('authorized' if number not in rotisserie_unauthorized else 'not-authorized')
        ),
    }
    if include_dynamic_gates:
        gates['open_dependency_blocker'] = issue_has_open_blocker(number)
        gates['worker_specific_note'] = (
            'strike-retry producer exclusion is per worker and evaluated at assignment'
        )
    cutover_allowed = eligibility.rust_cutover_work_allowed(
        labels_of(payload), str(payload.get('body') or ''),
        cutover_open=cutover_open,
    )
    gates['rust_cutover_allowed'] = cutover_allowed
    report: dict[str, Any] = {
        'issue': number,
        'eligible': verdict.eligible and cutover_allowed,
        'reasons': list(verdict.reasons) + (
            [] if cutover_allowed else ['Rust cutover #4335 is still open']
        ),
        'labels': sorted(labels_of(payload)),
        'open_canonical_prs': owning_prs,
        'dynamic_gates': gates,
    }
    if verdict.eligible and include_dynamic_gates and gates['open_dependency_blocker']:
        report['eligible'] = False
        report['reasons'] = [
            reason
            for reason in verdict.reasons
            if not reason.startswith('eligible: ')
        ] + ['a live open dependency blocker is reported by GitHub']
    return report


def render_explain_report(report: dict[str, Any]) -> str:
    """Render an ``explain_issue`` report as operator-readable plain text.

    Args:
        report: The report returned by :func:`explain_issue`.

    Returns:
        Human-readable lines naming the verdict, each reason, and each gate.
    """
    verdict = 'eligible' if report['eligible'] else 'not eligible'
    lines = [f"#{report['issue']}: {verdict}"]
    lines.extend(f"  - {reason}" for reason in report['reasons'])
    prs = report.get('open_canonical_prs') or []
    lines.append(f"  open canonical PRs: {', '.join(f'#{n}' for n in prs) if prs else 'none'}")
    lines.append(f"  labels: {', '.join(report['labels']) or 'none'}")
    for name, value in report.get('dynamic_gates', {}).items():
        lines.append(f"  gate {name}: {value}")
    return '\n'.join(lines)


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


def dispatch_target(kind: str, number: int, action: str) -> dict[str, object]:
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
    _view, health = durable_factory_state()
    capacity = capacity_report(load_manifest_candidates(), health, now_epoch=int(time.time()))
    workers = cast(list[dict[str, str]], capacity['executable_candidates'])
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
            run_gh(['workflow', 'run', 'free-model-factory-entry.yml', '--repo', REPO,
                    '--ref', 'main', '-f', f'worker={worker}',
                    '-f', 'assignment=' + json.dumps(assignment_context(candidate, worker))])
        except RuntimeError:
            # A timed-out POST can already have queued the worker. Keep its lease
            # so another manual request cannot start duplicate work.
            raise RuntimeError('Could not confirm worker dispatch. Check Actions before retrying.') from None
        return {
            'kind': candidate.kind,
            'number': candidate.number,
            'action': selected if action == 'advance' else action,
            'status': 'dispatched',
            'worker': worker,
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
    target_parser = subparsers.add_parser('dispatch-target')
    target_parser.add_argument('--kind', required=True, choices=('issue', 'pr'))
    target_parser.add_argument('--number', required=True, type=int)
    target_parser.add_argument('--action', required=True, choices=(
        'advance', 'implement', 'merge-conflict', 'ci-repair', 'review-findings', 'review',
    ))
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
    subparsers.add_parser('signal-completion')
    subparsers.add_parser('signal-roster')
    args = parser.parse_args()
    if args.command == 'snapshot':
        print(json.dumps({'issues': list_issues(), 'prs': list_prs()}))
        return 0
    if args.command == 'reconcile':
        released = reconcile_stale_leases()
        repaired = reconcile_contradictory_labels()
        print(json.dumps({'released': released, 'contradictions_repaired': repaired}))
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
            print(json.dumps({'kind': 'none', 'reason': 'worker_unavailable'}))
            return 0
        candidate = assign_from_pool(
            args.worker,
            kinds=tuple(args.kinds) if args.kinds else None,
            batch_reconciled=args.batch_reconciled,
        )
        if candidate is None:
            print(json.dumps({'kind': 'none', 'reason': 'no_work'}))
            return 0
        print(json.dumps(assignment_context(candidate, args.worker)))
        return 0
    if args.command == 'dispatch-target':
        print(json.dumps(dispatch_target(args.kind, args.number, args.action)))
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
    # labels_pending: the Neon claim is discharged but GitHub labels still need
    # stale-lease reconciliation; keep the job red so the gap stays visible.
    return 1 if outcome.get('status') in ('failed', 'deferred', 'missing_claim', 'labels_pending') else 0

if __name__ == "__main__":
    raise SystemExit(main())
