#!/usr/bin/env python3
"""Derive durable factory worker identities from provider integrations.

Model IDs are never inputs to slot identity, and no checked-in roster or
expected-worker lock is loaded. Source names and endpoints are protocol
integration metadata; runtime catalogs determine executable model candidates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from collections.abc import Mapping

from factory_provider_registry import PROVIDERS

# These are OpenCode-native execution integrations rather than OpenAI-compatible
# endpoints. They are not model identifiers or a list of permitted models.
NATIVE_SOURCES = ("opencode-free", "openrouter-free", "kilo-auto", "omniroute-free")
IDENTITY_NAMESPACE = 1_000_000_000_000
IDENTITY_MULTIPLIER = 100
MAX_SLOTS_PER_SOURCE = 99


@dataclass(frozen=True)
class WorkerSlot:
    worker: str
    source: str
    index: int
    scheduler: str = "dispatcher"

    def as_record(self) -> dict[str, str]:
        return {
            "worker": self.worker,
            "source": self.source,
            "minute": str((self.index * 5) % 60),
            "scheduler": self.scheduler,
            "display_name": f"{self.source} catalog slot {self.index + 1}",
        }


def sources() -> tuple[str, ...]:
    """Discover integrated providers, not models or a curated worker inventory."""
    return tuple(sorted({*PROVIDERS, *NATIVE_SOURCES}))


def slot_count(env: Mapping[str, str] | None = None) -> int:
    """Provider concurrency capacity controls slots, not the model catalog size."""
    values = os.environ if env is None else env
    raw = values.get("FACTORY_SLOTS_PER_PROVIDER", "12")
    try:
        slots = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("factory slot capacity must be a positive integer") from exc
    if not 1 <= slots <= MAX_SLOTS_PER_SOURCE:
        raise ValueError("factory slot capacity out of supported range")
    return slots


def worker_id(source: str, index: int) -> str:
    """Content-independent, stable decimal lease/label identity."""
    if source not in sources():
        raise ValueError(f"unknown factory integration: {source}")
    if not 0 <= index < MAX_SLOTS_PER_SOURCE:
        raise ValueError("factory slot index out of bounds")
    namespace = int.from_bytes(hashlib.sha256(source.encode()).digest()[:4], "big")
    return str(IDENTITY_NAMESPACE + namespace * IDENTITY_MULTIPLIER + index)


def slots(env: Mapping[str, str] | None = None) -> tuple[WorkerSlot, ...]:
    count = slot_count(env)
    values = tuple(
        WorkerSlot(worker_id(source, index), source, index)
        for source in sources() for index in range(count)
    )
    if len({item.worker for item in values}) != len(values):
        raise ValueError("worker identities collided across provider integrations")
    return values


def resolve(worker: str, env: Mapping[str, str] | None = None) -> WorkerSlot:
    """Resolve only a current integrated slot; never infer a model ID."""
    for item in slots(env):
        if item.worker == worker:
            return item
    raise ValueError("unknown or inactive worker slot")


def offer(tick: int, env: Mapping[str, str] | None = None) -> list[str]:
    """Rotate by source, then slot; capability and health filters run afterward."""
    count = slot_count(env)
    source_names = sources()
    rotated = source_names[tick % len(source_names):] + source_names[:tick % len(source_names)]
    offset = (tick // len(source_names)) % count
    return [
        worker_id(source, (offset + index) % count)
        for index in range(count)
        for source in rotated
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("list", "resolve", "offer", "sources"))
    parser.add_argument("--worker")
    parser.add_argument("--tick", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.command == "sources":
            value = list(sources())
        elif args.command == "list":
            value = [item.as_record() for item in slots()]
        elif args.command == "resolve":
            if not args.worker:
                parser.error("--resolve requires --worker")
            value = resolve(args.worker).as_record()
        else:
            value = offer(args.tick)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
