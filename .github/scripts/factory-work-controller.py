#!/usr/bin/env python3
"""Deterministic GitHub assignment and lease reconciliation for ComicPile."""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, cast
sys.path.insert(0, os.path.dirname(__file__))
# OmniRoute enable gate: FACTORY_OMNIROUTE_ENABLED (default off).
from factory_capacity_policy import (
    DEFAULT_MULTI_PROVIDER_ENTRY_CAP,
    DEFAULT_OMNIROUTE_FREE_ENTRY_CAP,
    omniroute_enabled,
    remaining_omniroute_free_entry_slots,
)
from factory_work_policy import (BLOCKED_LABELS, FACTORY_NO_DIFF_RETRY_RESET_SECONDS, FIXED_LEASE_TTL_SECONDS, FIXED_OWNER_RE, NoDiffAttempt, OWNER_RE, REQUIRED_CHECK_FAILURE_STATES, STAGE_LABELS, STAGE_PRECEDENCE, Candidate, build_candidates, comment_is_trusted, env_positive_int, item_is_unowned, issue_explicitly_closed_by_pr, labels_of, lease_is_stale, linked_issue_from_branch, linked_issue_from_pr, order_candidates_for_worker, owner_of, parse_no_diff_attempts_from_comments, plan_distinct_assignments)
from stale_pr_decay import StalePRGuard
REPO = os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
GH_TIMEOUT_SECONDS = env_positive_int("FACTORY_GH_TIMEOUT_SECONDS", 120)
ASSIGNMENT_WRITER_WORKFLOW_PATH = ".github/workflows/fixed-model-factory-dispatch.yml"
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
__all__ = ["linked_issue_from_branch", "plan_distinct_assignments"]


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
    """List open issues visible to the assignment controller."""
    return cast(list[dict[str, Any]], gh_json(['issue', 'list', '--repo', REPO, '--state', 'open', '--limit', '1000', '--json', 'number,title,body,labels,createdAt,updatedAt']))


def list_prs() -> list[dict[str, Any]]:
    """List open pull requests including current mergeability."""
    return cast(list[dict[str, Any]], gh_json(['pr', 'list', '--repo', REPO, '--state', 'open', '--limit', '500', '--json', 'number,title,body,labels,headRefName,headRefOid,createdAt,updatedAt,isDraft,mergeable,mergeStateStatus']))


def target_json(number: int) -> dict[str, Any]:
    """Fetch one issue-compatible GitHub target payload."""
    return cast(dict[str, Any], gh_json(['api', f'repos/{REPO}/issues/{number}']))


def replace_factory_labels(number: int, owner: str, stage: str | None=None) -> None:
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
    current = [label['name'] for label in target.get('labels', [])]
    existing_stage = next((label for label in current if label in STAGE_LABELS), None)
    stage = stage or existing_stage or 'factory:building'
    # Remove all existing owner labels (including factory:unowned) and stage labels
    labels = [label for label in current if not OWNER_RE.fullmatch(label) and label not in STAGE_LABELS and (label != 'factory')]
    labels.extend(['factory', owner, stage])
    run_gh(['api', '--method', 'PUT', f'repos/{REPO}/issues/{number}/labels', '--input', '-'], input_json={'labels': sorted(set(labels))})


def issue_has_open_blocker(number: int) -> bool:
    """Return whether an issue has an open dependency blocker."""
    try:
        blockers = cast(list[dict[str, Any]], gh_json(['api', f'repos/{REPO}/issues/{number}/dependencies/blocked_by?per_page=100']) or [])
    except RuntimeError:
        return True
    return any(item.get('state') == 'open' for item in blockers)


def required_checks_failed(pr_number: int) -> bool:
    """Return whether any required pull-request check has failed."""
    try:
        output = run_gh(['pr', 'checks', str(pr_number), '--repo', REPO, '--required', '--json', 'state'], check=False)
    except RuntimeError:
        return False
    if not output.strip():
        return False
    try:
        checks = cast(list[dict[str, Any]], json.loads(output))
    except json.JSONDecodeError:
        return False
    return any(str(check.get('state', '')).upper() in REQUIRED_CHECK_FAILURE_STATES for check in checks)


def candidate_is_live_executable(candidate: Candidate) -> bool:
    """Return whether a ranked candidate has an executable next action."""
    if candidate.kind == 'issue':
        return not issue_has_open_blocker(candidate.number)
    target = target_json(candidate.number)
    labels = {label['name'] for label in target.get('labels', [])}
    if 'factory:ready' in labels:
        return False
    if candidate.conflicted:
        return True
    if 'factory:ci' in labels:
        return required_checks_failed(candidate.number)
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


def record_controller_lease_activity(number: int, worker: str, kind: str) -> None:
    """Persist a trusted lease timestamp before dispatcher handoff."""
    epoch = int(time.time())
    marker = f'<!-- comic-pile-factory-controller-claim-v1:{kind}-{number}:{worker}:{epoch} -->'
    run_gh(['issue', 'comment', str(number), '--repo', REPO, '--body', marker])


def assign_candidate(candidate: Candidate, worker: str) -> bool:
    """Claim a candidate and any linked issue for one fixed-model worker.

    Enforces the invariant: an executable issue must never simultaneously satisfy
    both 'factory:unowned' and active factory ownership.
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

    if worker_has_active_lease(worker):
        return False

    def release_verified_claims(claimed_numbers: list[int]) -> None:
        """Release only labels this worker can still prove it owns."""
        for claimed_number in claimed_numbers:
            try:
                if target_owned_by(claimed_number, owner):
                    replace_factory_labels(claimed_number, 'factory:unowned')
            except Exception:
                pass
    claimed: list[int] = []
    try:
        for number in numbers:
            # Revalidate immediately before the first lease mutation so a stale
            # idle snapshot cannot create a second lease.
            if not target_still_unowned(number):
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
                record_controller_lease_activity(number, worker, kind)
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


def recent_factory_comments(now_epoch: int) -> list[dict[str, Any]]:
    """Fetch comments that can still contribute to the no-diff budget."""
    since_epoch = max(0, now_epoch - FACTORY_NO_DIFF_RETRY_RESET_SECONDS)
    since = datetime.fromtimestamp(since_epoch, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
    pages = gh_json(['api', '--paginate', '--slurp', f'repos/{REPO}/issues/comments?since={since}&per_page=100'])
    return flatten_pages(pages)


def load_no_diff_attempt_records(now_epoch: int | None=None) -> list[NoDiffAttempt]:
    """Load durable no-diff attempts still inside the rolling retry window."""
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    return parse_no_diff_attempts_from_comments(recent_factory_comments(now_epoch), now_epoch=now_epoch)


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
    for status in ('queued', 'in_progress'):
        try:
            pages = gh_json(['api', '--paginate', '--slurp', f'repos/{REPO}/actions/workflows/free-model-factory-entry.yml/runs?status={status}&per_page=100'])
        except RuntimeError:
            unresolved_run_ids.add(f'{status}-run-query-unavailable')
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
            pages = gh_json(['api', '--paginate', '--slurp', f'repos/{REPO}/issues/1093/comments?per_page=100'])
        except RuntimeError:
            return ActiveWorkerResult(workers, unresolved_run_ids)
        if not isinstance(pages, list):
            return ActiveWorkerResult(workers, unresolved_run_ids)
        for comment in flatten_pages(pages):
            if not comment_is_trusted(comment):
                continue
            body = str(comment.get('body') or '')
            run_match = re.search(r'(?m)^Run:\s*(\d+)\s*$', body)
            worker_match = re.search(r'(?m)^Worker:\s*opencode-free-model-factory-(\d+)\s*$', body)
            if run_match and worker_match and (run_match.group(1) in numeric_unresolved):
                workers.add(int(worker_match.group(1)))
                unresolved_run_ids.discard(run_match.group(1))
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
    """Repair pre-existing contradictory ownership labels without fresh assignment.

    A target carrying both ``factory:unowned`` and exactly one active worker
    lease is contradictory: it claims to be simultaneously unowned and owned.
    Remove the redundant ``factory:unowned`` label while preserving the active
    owner and workflow stage. Fail closed (repair nothing) when the legitimate
    owner cannot be determined, e.g. two distinct active worker leases.
    """
    issue_items = list_issues() if issues is None else issues
    pr_items = list_prs() if prs is None else prs
    repaired: list[int] = []
    for item in [*issue_items, *pr_items]:
        owners = [label for label in labels_of(item) if OWNER_RE.fullmatch(label)]
        if len(owners) <= 1 or 'factory:unowned' not in owners:
            continue
        active_owners = [owner for owner in owners if owner != 'factory:unowned']
        if len(active_owners) != 1:
            continue
        number = int(item['number'])
        replace_factory_labels(number, active_owners[0])
        repaired.append(number)
        print(
            f'[factory-controller] reconciled contradictory unowned label on #{number}',
            file=sys.stderr,
        )
    return repaired


def reconcile_stale_leases(now_epoch: int | None=None) -> list[int]:
    """Release only leases whose inactivity and age are both provable."""
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    active_result = active_fixed_workers()
    if isinstance(active_result, tuple):
        active, unresolved_runs = active_result
    else:
        active, unresolved_runs = active_result, set()
    if unresolved_runs:
        print('[factory-controller] retaining fixed leases because active run identity is unresolved: ' + ', '.join(sorted(unresolved_runs)), file=sys.stderr)
    released: list[int] = []
    for number, owner in owned_targets():
        fixed_owner = FIXED_OWNER_RE.fullmatch(owner)
        if fixed_owner and (unresolved_runs or int(fixed_owner.group('worker')) in active):
            continue
        activity = latest_lease_activity_epoch(number)
        if not lease_is_stale(owner, active_fixed_workers=active, has_unresolved_active_runs=bool(unresolved_runs), latest_activity_epoch=activity, now_epoch=now_epoch):
            continue
        replace_factory_labels(number, 'factory:unowned')
        released.append(number)
        print(f'[factory-controller] released stale {owner} lease on #{number}', file=sys.stderr)
    return released


def worker_has_active_lease(worker: str) -> bool:
    """Return whether a fixed-model worker already owns open work."""
    owner = f'factory:{worker}'
    return any(current_owner == owner for _, current_owner in owned_targets())


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


def assign(worker: str, kinds: tuple[str, ...] | None=None) -> Candidate | None:
    """Assign the highest-ranked executable work to one fixed-model worker.

    When kinds is provided, only candidates whose kind is in the tuple
    are considered. This allows completion drains to request dispatcher
    allocation restricted to specific candidate types.
    """
    if not re.fullmatch('(?:[6-9]|[1-3][0-9]|[4-7][0-9])', worker):
        raise SystemExit(f'unsupported fixed-model worker: {worker}')
    if worker_has_active_lease(worker):
        print(f'[factory-controller] Factory {worker} already has an active lease; skipping dispatch', file=sys.stderr)
        return None
    snapshot = omniroute_free_entry_capacity()
    if snapshot['remaining'] <= 0:
        print(
            '[factory-controller] OmniRoute free-entry cap reached '
            f'(in_flight={snapshot["in_flight"]} cap={snapshot["cap"]}); skipping dispatch',
            file=sys.stderr,
        )
        return None
    reconcile_stale_leases()
    issues = list_issues()
    prs = list_prs()
    try:
        attempt_records: list[NoDiffAttempt] | None = load_no_diff_attempt_records()
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
    if kinds is not None:
        candidates = [candidate for candidate in candidates if candidate.kind in kinds]
    elif attempt_records is None:
        candidates = [candidate for candidate in candidates if candidate.kind == 'pr']
    candidates = order_candidates_for_worker(candidates, worker)
    for candidate in candidates:
        if not candidate_is_live_executable(candidate):
            continue
        if (
            candidate.kind == "issue"
            and issue_excludes_worker_on_strike_retry(candidate.number, worker)
        ):
            continue
        if assign_candidate(candidate, worker):
            reason = ' conflict-repair' if candidate.conflicted else ''
            print(f'[factory-controller] assigned {candidate.kind} #{candidate.number} lane={candidate.lane} priority={candidate.priority}{reason} to Factory {worker}', file=sys.stderr)
            return candidate
    return None


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


def release_worker(worker: str, reason: str = 'controller-release') -> list[int]:
    """Release all targets still owned by one fixed-model worker immediately.

    Used by dispatcher start-failures and by Entry pre-session abort (smoke,
    executor selection, missing credentials). Preserves the truthful workflow
    stage so a ``factory:review`` PR stays reviewable without waiting for the
    900s stale-lease TTL. Posts ``comic-pile-factory-claim-released-v3``.

    Factory 10 run 33990134163 and Factory 42 run 33991283563 are the
    regression fixture: both smoked ``auto/coding:free`` after claiming PR
    #2235 and died before ``free-model-factory-worker.sh``, leaving
    ``factory:<worker>`` + ``factory:review`` until TTL expiry.
    """
    owner = f'factory:{worker}'
    released: list[int] = []
    issues = list_issues()
    prs = list_prs()
    targets: list[tuple[int, str]] = []
    for item in issues:
        if owner_of(labels_of(item)) == owner:
            targets.append((int(item['number']), 'issue'))
    for item in prs:
        if owner_of(labels_of(item)) == owner:
            targets.append((int(item['number']), 'pr'))
    for number, kind in targets:
        replace_factory_labels(number, 'factory:unowned')
        try:
            record_claim_released(number, worker, kind, reason)
        except Exception:
            pass
        released.append(number)
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


def check_stale_prs(dry_run: bool = False) -> dict[str, Any]:
    """Evaluate open Factory PRs for staleness and expire any that cross the threshold.

    Returns a dict with the baseline statistics and expiration results.
    This integrates the adaptive stale-PR decay guard into the controller.
    """
    guard = StalePRGuard()
    prs = list_prs()
    baseline = guard.get_baseline(prs)
    results = guard.process_stale_prs(prs=prs, dry_run=dry_run)
    expired = [r for r in results if r.get("status") == "expired"]
    return {
        "baseline": baseline,
        "results": results,
        "expired_count": len(expired),
        "dry_run": dry_run,
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
    inspect_parser = subparsers.add_parser('inspect')
    inspect_parser.add_argument('--worker', required=True)
    release_parser = subparsers.add_parser('release')
    release_parser.add_argument('--worker', required=True)
    release_parser.add_argument('--reason', default='controller-release')
    recovery_parser = subparsers.add_parser('conflict-recovery')
    recovery_parser.add_argument('--worker', required=True)
    args = parser.parse_args()
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
        candidate = assign(args.worker, kinds=tuple(args.kinds) if args.kinds else None)
        if candidate is None:
            print(json.dumps({'kind': 'none'}))
            return 0
        print(json.dumps({'kind': candidate.kind, 'number': candidate.number, 'lane': candidate.lane, 'priority': candidate.priority, 'linked_issue': candidate.linked_issue, 'conflicted': candidate.conflicted}))
        return 0
    if args.command == 'inspect':
        print(json.dumps(inspect_assignment(args.worker)))
        return 0
    if args.command == 'conflict-recovery':
        result = selectivity_conflict_recovery(args.worker)
        print(json.dumps(result))
        return 0
    print(json.dumps({'released': release_worker(args.worker, reason=args.reason)}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
