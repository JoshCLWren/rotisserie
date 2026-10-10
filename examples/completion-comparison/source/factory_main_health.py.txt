#!/usr/bin/env python3
"""Summarize product CI workflow runs for a main-branch commit."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping

FAILING_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "startup_failure",
    "timed_out",
}


def workflow_runs(payload: object) -> Iterable[Mapping[str, object]]:
    """Yield workflow runs from one REST page or a slurped page list.

    Args:
        payload: Decoded GitHub workflow-runs REST response.

    Yields:
        Mapping objects representing workflow runs.

    Raises:
        ValueError: The response does not have the expected GitHub shape.

    """
    pages: list[object]
    if isinstance(payload, list):
        pages = payload
    else:
        pages = [payload]

    for page in pages:
        if not isinstance(page, Mapping):
            raise ValueError("workflow-runs response page must be an object")
        runs = page.get("workflow_runs")
        if not isinstance(runs, list):
            raise ValueError("workflow-runs response is missing workflow_runs")
        for run in runs:
            if not isinstance(run, Mapping):
                raise ValueError("workflow run must be an object")
            yield run


def summarize(payload: object, sha: str) -> dict[str, object]:
    """Return the exact-SHA product CI health summary.

    Factory workflows also execute against ``main`` and create check runs on
    the same SHA. Restricting health to push-triggered ``CI`` workflow runs
    prevents provider and controller failures from masquerading as product
    regressions.

    Args:
        payload: Decoded, optionally paginated workflow-runs response.
        sha: Exact main commit SHA being evaluated.

    Returns:
        JSON-serializable counts and failing-run descriptions.

    """
    relevant: dict[int, Mapping[str, object]] = {}
    for run in workflow_runs(payload):
        if run.get("name") != "CI" or run.get("event") != "push":
            continue
        if run.get("head_sha") != sha:
            continue
        run_id = run.get("id")
        if not isinstance(run_id, int):
            raise ValueError("workflow run is missing its numeric id")
        relevant[run_id] = run

    failures: list[str] = []
    for run_id, run in sorted(relevant.items()):
        conclusion = str(run.get("conclusion") or "").lower()
        if conclusion in FAILING_CONCLUSIONS:
            failures.append(f"CI run {run_id} ({conclusion})")

    return {
        "failing": len(failures),
        "failing_names": ", ".join(failures),
        "total": len(relevant),
    }


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed command-line namespace.

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    return parser.parse_args()


def main() -> int:
    """Read workflow-run JSON from stdin and print a health summary.

    Returns:
        Process exit status.

    """
    args = parse_args()
    try:
        payload = json.load(sys.stdin)
        summary = summarize(payload, args.sha)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"invalid workflow-runs response: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
