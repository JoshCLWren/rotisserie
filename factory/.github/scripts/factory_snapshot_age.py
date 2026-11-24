#!/usr/bin/env python3
"""Print the age in seconds of a published snapshot's generated_at stamp.

A missing or unreadable stamp prints a very large age, so callers treat it
as stale and publish a fresh snapshot.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

UNKNOWN_AGE = 10**9


def age_seconds(generated_at: str, now: datetime | None = None) -> int:
    try:
        stamp = datetime.fromisoformat(generated_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return UNKNOWN_AGE
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0, int(((now or datetime.now(UTC)) - stamp).total_seconds()))


def main(argv: list[str]) -> int:
    print(age_seconds(argv[1] if len(argv) > 1 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
