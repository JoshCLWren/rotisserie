"""Why one assignment pass leased nothing: each skipped candidate and its reason.

The controller notes every rejection here, and the ``assign`` command returns
the summary with ``kind: none`` so the dispatcher log names the blocker.
"""

from __future__ import annotations

from datetime import UTC, datetime

_SKIPS: list[tuple[int, str]] = []


def reset() -> None:
    _SKIPS.clear()


def skip(number: int, reason: str) -> bool:
    """Record why ``number`` was not leased; always False, so callers can return it."""
    _SKIPS.append((number, reason))
    return False


def summary(limit: int = 8) -> str:
    """One line: the first rejection reason per item, in the order they were tried."""
    if not _SKIPS:
        return "no candidate passed the queue gates (backlog, retry budgets, cooling or intake)"
    first: dict[int, str] = {}
    for number, reason in _SKIPS:
        first.setdefault(number, reason)
    parts = [f"#{number}: {reason}" for number, reason in list(first.items())[:limit]]
    hidden = len(first) - limit
    return "; ".join(parts) + (f"; and {hidden} more" if hidden > 0 else "")


def store_conflict(repo: str, worker: str, numbers: list[int]) -> str:
    """Name the store claim that refused a lease, or the capacity checks when none holds a key."""
    from factory_state_ledger import target_lease, worker_claim

    held = []
    try:
        claim = worker_claim(repo, worker, active_only=True)
        if claim is not None:
            held.append(f"worker {worker} holds claim {claim[0][:8]} on {claim[1]}")
        for number in numbers:
            row = target_lease(repo, number)
            if row is not None:
                until = datetime.fromisoformat(row["expires_at"]).astimezone(UTC)
                holder = row["worker"] or row["operation"] or "an outside claim"
                held.append(f"#{number} held by {holder} until {until:%H:%MZ}")
    except Exception as exc:  # noqa: BLE001 - the explanation must not hide the refusal
        return f"store refused the lease (holder unreadable: {type(exc).__name__})"
    if held:
        return "store lease held: " + ", ".join(held)
    return "store refused the lease (provider budget, daily cap or worker eligibility)"
