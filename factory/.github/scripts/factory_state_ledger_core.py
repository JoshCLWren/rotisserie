"""Lease key formats shared by the factory store modules."""
from __future__ import annotations

import re

WORKER_RE = re.compile(r"^[0-9]+$")
LEASE_PREFIX = "factory-lease-v1:"


def _keys(repo: str, worker: str, numbers: list[int]) -> list[str]:
    return [f"{LEASE_PREFIX}{repo}:worker:{worker}"] + [
        f"{LEASE_PREFIX}{repo}:target:{number}" for number in sorted(set(numbers))
    ]


def _target_key(repo: str, number: int) -> str:
    return f"{LEASE_PREFIX}{repo}:target:{number}"
