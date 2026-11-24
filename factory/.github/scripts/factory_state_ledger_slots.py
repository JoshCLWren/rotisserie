"""Slot leases: several workers on one target, exactly one holder per slot.

A normal lease lets one worker hold an issue or PR. Incident racing (#239)
needs several separate fix attempts on one issue, and a later collaborative
mode needs one lease per planned piece. Both are slots of one target:

    factory-lease-v1:<repo>:slot:<number>:attempt-2
    factory-lease-v1:<repo>:slot:<number>:piece-api-schema

``acquire_slot`` (``factory_turso_slots``) is the only allocator. In one write
batch it takes the first free slot from the caller's ordered list, writes the slot row
and the worker row with one claim ID, and reserves provider capacity like a
normal lease. A worker holds at most one claim. Siblings on the same target use
distinct providers unless the caller allows a shared one. The normal
``release_lease`` and ``renew_verified_workers`` release and renew slot rows by
claim ID, so a slot claim needs no separate release path. An expired slot is
free again, like an expired worker lease: renewal keeps a live run's slot.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from factory_state_ledger_core import LEASE_PREFIX, WORKER_RE

SLOT_RE = re.compile(r"^(?:attempt-[1-9][0-9]?|piece-[a-z0-9][a-z0-9-]{0,39})$")
MAX_SLOTS = 32
DISTINCT_CHOICES = ("provider", "none")


def slot_prefix(repo: str, number: int) -> str:
    return f"{LEASE_PREFIX}{repo}:slot:{number}:"


def slot_key(repo: str, number: int, slot: str) -> str:
    return slot_prefix(repo, number) + slot


def attempt_slots(count: int) -> list[str]:
    return [f"attempt-{index}" for index in range(1, count + 1)]


def _validate(worker: str, number: int, slots: Sequence[str], ttl: int, distinct: str) -> None:
    if not WORKER_RE.fullmatch(worker) or number <= 0:
        raise ValueError("invalid factory slot worker or target")
    if not slots or len(slots) > MAX_SLOTS or len(set(slots)) != len(slots):
        raise ValueError("invalid factory slot list")
    if any(not SLOT_RE.fullmatch(slot) for slot in slots):
        raise ValueError("invalid factory slot name")
    if not 60 <= ttl <= 86400:
        raise ValueError("invalid factory lease TTL")
    if distinct not in DISTINCT_CHOICES:
        raise ValueError("invalid factory slot distinctness rule")
