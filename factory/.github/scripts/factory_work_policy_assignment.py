"""Per-worker candidate ordering and review capacity for the factory work policy."""
from __future__ import annotations

from factory_work_policy_priority import Candidate


def review_share_for_backlog(review_backlog: int) -> float:
    """Return the review-first worker share for the current completion pressure.

    Use a continuous ratio: 25% at idle, rising to 90% by three pending
    completion PRs. Reserve reviewers before new issue intake can outrun them.
    """
    depth = max(0, int(review_backlog))
    return min(0.90, 0.25 + (0.65 * min(depth, 3) / 3.0))


def review_capacity_worker(worker: str, *, review_backlog: int = 0) -> bool:
    """Return whether this worker slot prioritizes review under current pressure.

    Membership in the review-first cohort is deterministic from the worker id and
    the ratio from :func:`review_share_for_backlog`.
    """
    share = review_share_for_backlog(review_backlog)
    # Multiply by a fixed odd constant so nearby worker ids do not clump.
    return (int(worker) * 37) % 100 < int(round(share * 100))


def candidate_is_independent_for_worker(candidate: Candidate, worker: str) -> bool:
    """Prevent a contributing factory from being assigned semantic review of its PR.

    Issue #3059 makes every worker that authored the exact head ineligible to
    attest it, not only the worker that opened the PR. Excluding contributors at
    selection time is what lets queue selection hand the head to another
    eligible reviewer automatically instead of churning the same worker
    through a controller refusal.
    """
    if candidate.kind == 'pr' and candidate.is_fork:
        # Fork execution and review authorization are independent of model IDs.
        from factory_worker_slots import resolve

        try:
            source = resolve(worker).source
        except ValueError:
            return False
        if candidate.stage == 'factory:review':
            if source == 'kilo-auto':
                return False
        elif source != 'kilo-auto':
            return False
    if candidate.kind != 'pr' or candidate.stage != 'factory:review':
        return True
    if candidate.producer_worker == worker:
        return False
    return worker not in candidate.head_contributors


def worker_delivery_role(worker: str) -> str:
    """No provider/model pins dictate intake or completion preferences."""
    del worker
    return ''


def order_candidates_for_worker(candidates: list[Candidate], worker: str) -> list[Candidate]:
    """Balance completion pressure with a deterministic fresh-issue intake lane."""
    eligible = [
        candidate
        for candidate in candidates
        if candidate_is_independent_for_worker(candidate, worker)
    ]
    review_backlog = sum(1 for c in candidates if c.kind == "pr" and c.stage in ("factory:review", "factory:ci", "factory:changes-requested"))
    review_first = review_capacity_worker(worker, review_backlog=review_backlog)
    role = worker_delivery_role(worker)

    def role_preference(candidate: Candidate) -> int:
        # Protected main-breakage and user-reported lanes outrank role preferences.
        if candidate.lane <= 1:
            return candidate.lane
        if (
            review_first
            and candidate.kind == 'pr'
            and candidate.stage == 'factory:review'
            and not candidate.conflicted
        ):
            return 1
        if role == 'starter' and candidate.kind == 'issue':
            return 2
        if role == 'finisher' and candidate.kind == 'pr' and candidate.stage in {
            'factory:changes-requested', 'factory:ci',
        }:
            return 2
        return 3

    def work_class(candidate: Candidate) -> int:
        if candidate.lane == 0:
            return 0
        if candidate.kind == 'pr':
            # A clean review-stage PR is one independent approval from merge.
            # Review-first workers finish it ahead of everything but lane 0;
            # non-review workers preserve product capacity (issue first).
            if candidate.stage == 'factory:review' and not candidate.conflicted:
                return 1 if review_first else 6
            if candidate.conflicted:
                return 2 if review_first else 8
            if candidate.stage == 'factory:ci':
                return 3
            if candidate.stage == 'factory:changes-requested':
                return 4
            return 5 if review_first else 9
        if candidate.lane == 1:
            return 6 if review_first else 4
        return 7 if review_first else 5

    return sorted(
        eligible,
        key=lambda candidate: (role_preference(candidate), work_class(candidate), candidate.sort_key()),
    )


def plan_distinct_assignments(candidates: list[Candidate], workers: list[str]) -> dict[str, Candidate]:
    """Pure helper used by regression coverage for one dispatcher batch."""
    remaining = list(candidates)
    assignments: dict[str, Candidate] = {}
    for worker in workers:
        ordered = order_candidates_for_worker(remaining, worker)
        if not ordered:
            continue
        selected = ordered[0]
        assignments[worker] = selected
        remaining.remove(selected)
    return assignments
