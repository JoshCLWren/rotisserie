"""External agent claim identities and request validation, shared by the store modules."""
from __future__ import annotations

import re

from factory_state_ledger_core import LEASE_PREFIX


# External agents share the same target keys as
# factory workers. They do NOT impersonate numeric worker slots or spend model
# provider reservations. An expired external claim is intentionally still a
# hold until its identity-fenced release or verified operator reconciliation.
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Review is offered since #191: the trusted bridge records the outside
# reviewer's declared model, so the merge gate can check its independence.
EXTERNAL_ACTIONS = frozenset({
    "implement", "review", "review-findings", "ci-repair", "merge-conflict",
})
EXTERNAL_WIP_ACTIONS = frozenset({
    "implement", "review-findings", "ci-repair", "merge-conflict",
})


def _external_request(repo: str, actor: str, client: str, numbers: list[int]) -> list[int]:
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not EXTERNAL_ID_RE.fullmatch(actor)
            or not EXTERNAL_ID_RE.fullmatch(client)):
        raise ValueError("invalid external actor, client or repository")
    if not numbers or len(numbers) > 2 or any(type(n) is not int or n < 1 for n in numbers):
        raise ValueError("invalid external lease targets")
    if len(set(numbers)) != len(numbers):
        raise ValueError("external lease targets must be distinct")
    return sorted(numbers)


def _external_key(repo: str, token: str) -> str:
    return f"{LEASE_PREFIX}{repo}:external:{token}"
