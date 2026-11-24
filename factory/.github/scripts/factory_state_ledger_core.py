"""Shared Neon lease ledger keys and configuration for the factory_state_ledger modules."""
from __future__ import annotations

import os
import re

WORKER_RE = re.compile(r"^[0-9]+$")
LEASE_PREFIX = "factory-lease-v1:"


def enabled() -> bool:
    return bool(os.environ.get("FACTORY_STATE_DSN_FILE"))


def _keys(repo: str, worker: str, numbers: list[int]) -> list[str]:
    return [f"{LEASE_PREFIX}{repo}:worker:{worker}"] + [
        f"{LEASE_PREFIX}{repo}:target:{number}" for number in sorted(set(numbers))
    ]


def prefix_pattern(prefix: str) -> str:
    """A LIKE pattern for keys starting with ``prefix``.

    Unlike ``starts_with()``, ``LIKE`` with a literal prefix can use the
    ``text_pattern_ops`` key index on every supported PostgreSQL version.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _target_key(repo: str, number: int) -> str:
    return f"{LEASE_PREFIX}{repo}:target:{number}"
