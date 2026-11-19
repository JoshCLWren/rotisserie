#!/usr/bin/env python3
"""Durable provider-quota pauses for fixed-model factory workers.

A paused worker stays on the roster (it is not retired) but is not offered
for dispatch or assignment until ``paused_until`` passes. After that the
normal smoke step re-proves the lane; a fresh quota failure means a new pause
entry is needed. Worker 46 is never pausable.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PAUSES_PATH = Path(__file__).resolve().parents[1] / "factory-quota-pauses.json"
PROTECTED_WORKERS = frozenset({46})


@dataclass(frozen=True)
class QuotaPause:
    """One worker paused until its provider quota resets."""

    worker: int
    provider: str
    paused_until: datetime
    reason: str
    reset_source: str


def quota_reset_epoch(provider: str, observed_epoch: int) -> int:
    """Use the UTC-day reset for OpenRouter, month for monthly providers.

    Other providers retain the short conservative fallback, since a generic
    quota outcome does not prove a calendar-wide account limit.
    """
    if provider == "openrouter-free":
        observed = datetime.fromtimestamp(observed_epoch, timezone.utc)
        next_day = observed.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return int(next_day.timestamp())
    if provider not in {"mistral", "ollama-cloud"}:
        return observed_epoch + 30 * 60
    observed = datetime.fromtimestamp(observed_epoch, timezone.utc)
    year = observed.year + (observed.month == 12)
    month = observed.month % 12 + 1
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())


def _parse_until(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"paused_until must be an ISO-8601 UTC timestamp ending in Z: {value!r}")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def load_pauses(path: Path | str = PAUSES_PATH) -> list[QuotaPause]:
    """Load and validate configured quota pauses; a missing file means none."""
    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported factory-quota-pauses schema_version")
    pauses: list[QuotaPause] = []
    seen: set[int] = set()
    for entry in data.get("pauses", []):
        worker = entry.get("worker")
        if not isinstance(worker, int) or isinstance(worker, bool):
            raise ValueError(f"pause worker must be an integer: {worker!r}")
        if worker in PROTECTED_WORKERS:
            raise ValueError(f"worker {worker} is protected and must never be paused")
        if worker in seen:
            raise ValueError(f"duplicate pause for worker {worker}")
        seen.add(worker)
        pauses.append(
            QuotaPause(
                worker=worker,
                provider=str(entry.get("provider", "")),
                paused_until=_parse_until(entry.get("paused_until")),
                reason=str(entry.get("reason", "")),
                reset_source=str(entry.get("reset_source", "")),
            )
        )
    return pauses


def active_pauses(
    now: datetime | None = None, pauses: Iterable[QuotaPause] | None = None
) -> dict[int, QuotaPause]:
    """Return pauses whose reset time has not yet passed, keyed by worker."""
    now = now or datetime.now(timezone.utc)
    items = load_pauses() if pauses is None else pauses
    return {pause.worker: pause for pause in items if pause.paused_until > now}


def is_paused(worker: int | str | None, now: datetime | None = None) -> QuotaPause | None:
    """Return the active pause for a worker, or None."""
    # Avoid tuple-except syntax that differs across Python versions; the
    # dispatcher runs this with the runner's system python3.
    text = str(worker).strip()
    if not text.isdigit():
        return None
    return active_pauses(now).get(int(text))


def describe(pause: QuotaPause) -> str:
    """Render a one-line operator log message for a paused worker."""
    until = pause.paused_until.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"Factory {pause.worker} paused until {until} ({pause.reset_source}): {pause.reason}"


def filter_workers(workers: list[str], now: datetime | None = None) -> tuple[list[str], list[str]]:
    """Split worker ids into (dispatchable, paused log lines), preserving order."""
    active = active_pauses(now)
    kept: list[str] = []
    messages: list[str] = []
    for worker in workers:
        pause = active.get(int(worker)) if str(worker).isdigit() else None
        if pause is None:
            kept.append(worker)
        else:
            messages.append(describe(pause))
    return kept, messages


def main(argv: list[str] | None = None) -> int:
    """CLI: ``filter`` reads a JSON worker array on stdin; ``list`` shows pauses."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("filter", "list", "validate"))
    args = parser.parse_args(argv)
    if args.command == "validate":
        load_pauses()
        return 0
    if args.command == "list":
        for pause in active_pauses().values():
            print(describe(pause))
        return 0
    workers = [str(item) for item in json.load(sys.stdin)]
    kept, messages = filter_workers(workers)
    for message in messages:
        print(message, file=sys.stderr)
    print(json.dumps(kept, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
