"""Controller operation lease identities and request validation."""
from __future__ import annotations

import re

from factory_state_ledger_core import WORKER_RE


OPERATION_HOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
# Controller operations (a branch sync) hold a target for seconds, not hours.
OPERATION_LEASE_TTL_SECONDS = 300


def _operation_request(holder: str, number: int, ttl: int) -> None:
    if not OPERATION_HOLDER_RE.fullmatch(holder) or WORKER_RE.fullmatch(holder):
        # A bare number would read as a worker ID in lease payloads and events.
        raise ValueError("invalid controller operation holder")
    if number <= 0:
        raise ValueError("invalid controller operation target")
    if not 30 <= ttl <= 3600:
        raise ValueError("invalid controller operation lease TTL")
