#!/usr/bin/env python3
"""Exclude unavailable provider integrations before a worker claims a lease."""
from __future__ import annotations

import json
import os
import sys

from factory_provider_registry import PROVIDERS
from factory_worker_slots import slots

ANONYMOUS = frozenset({"opencode-free", "openrouter-free", "kilo-auto"})
TRUTHY = frozenset({"true", "1", "yes", "on"})


def worker_sources() -> dict[str, str]:
    """Derive source assignment from stable worker IDs, never a roster file."""
    return {slot.worker: slot.source for slot in slots()}


def source_requirements(source: str) -> tuple[str, ...] | None:
    spec = PROVIDERS.get(source)
    if spec is not None:
        return tuple(f"FACTORY_HAS_{name}" for name in (spec.credential, *spec.additional_credentials))
    if source == "omniroute-free":
        return ("FACTORY_HAS_OMNIROUTE_API_KEY", "FACTORY_OMNIROUTE_ENABLED")
    return None


def filter_workers(
    workers: list[str], sources: dict[str, str], env: dict[str, str],
) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    deferred: list[str] = []
    for worker in workers:
        source = sources.get(worker, "")
        needs = source_requirements(source)
        if source in ANONYMOUS or (
            needs is not None and all(env.get(key, "").lower() in TRUTHY for key in needs)
        ):
            kept.append(worker)
        else:
            deferred.append(
                f"Factory {worker}: {source or 'unknown source'} missing provider credentials; no claim"
            )
    return kept, deferred


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("filter",))
    parser.parse_args()
    workers = json.load(sys.stdin)
    if not isinstance(workers, list) or any(
        not isinstance(value, str) or not value.isdecimal() for value in workers
    ):
        raise ValueError("expected JSON worker ID array")
    kept, deferred = filter_workers(workers, worker_sources(), dict(os.environ))
    for reason in deferred:
        print(reason, file=sys.stderr)
    print(json.dumps(kept))


if __name__ == "__main__":
    main()
