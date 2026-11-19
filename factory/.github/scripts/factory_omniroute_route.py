#!/usr/bin/env python3
"""Choose the native OmniRoute route that matches one factory assignment."""

from __future__ import annotations

import argparse
import os

CODING_ROUTE = "auto/coding:free"
REVIEW_ROUTE = "auto/reasoning:free"


def omniroute_enabled(raw: str | None = None) -> bool:
    """Return whether OmniRoute native auto/* routes may execute."""
    value = raw if raw is not None else os.environ.get("FACTORY_OMNIROUTE_ENABLED", "off")
    return value.strip().lower() in {"1", "on", "true", "yes"}


def route_for_assignment(mode: str, pr_stage: str = "") -> str:
    """Return the free OmniRoute intent route for the current factory work.

    Raises:
        RuntimeError: When OmniRoute is dark; callers must use multi-provider Entry.
    """
    if not omniroute_enabled():
        raise RuntimeError(
            "OmniRoute routes are disabled; use the multi-provider Entry path "
            "(nvidia, opencode-free, openrouter-free, kilo-auto) instead of auto/*"
        )
    if mode == "pr" and pr_stage == "factory:review":
        return REVIEW_ROUTE
    return CODING_ROUTE


def main() -> int:
    """Print the route selected for a worker assignment."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("issue", "pr"))
    parser.add_argument("--pr-stage", default="")
    args = parser.parse_args()
    print(route_for_assignment(args.mode, args.pr_stage))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
