#!/usr/bin/env python3
"""Report active worker heartbeat freshness without granting recovery authority."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

HEARTBEAT_RE = re.compile(r"<!-- factory-heartbeat:v1 worker=opencode-free-model-factory-(\d+) -->")
ATTEMPT_RE = re.compile(r"<!-- factory-attempt-outcome:v1 worker=opencode-free-model-factory-(\d+) -->")
STALE_SECONDS = 1200


def timestamp(value: str) -> datetime | None:
    """Parse registry UTC timestamps; invalid observations have no freshness."""
    try:
        parsed = datetime.fromisoformat(value.strip("` ").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def fields(body: str) -> dict[str, str]:
    """Read the registry's human-readable fields."""
    return {
        key.strip().casefold(): value.strip()
        for line in body.splitlines()
        if not line.startswith("<!--") and ":" in line
        for key, value in [line.split(":", 1)]
    }


def heartbeat_health(
    comments: Iterable[Mapping[str, object]],
    roster: Mapping[str, object],
    *,
    now: datetime,
    stale_seconds: int = STALE_SECONDS,
) -> dict[str, object]:
    """Return per-worker observations for expected, non-retired/non-paused workers.

    Inputs must already be filtered to trusted registry authors. Timestamp age
    is visibility only: stale means no recent observation, never a crash verdict.
    """
    expected = roster.get("expected_workers", [])
    excluded = [*roster.get("retired_workers", []), *roster.get("paused_workers", [])]
    active = {str(worker) for worker in expected} - {str(worker) for worker in excluded}
    heartbeats: dict[str, dict[str, str]] = {}
    attempts: dict[str, dict[str, str]] = {}
    for comment in comments:
        body = str(comment.get("body") or "")
        match = HEARTBEAT_RE.search(body) or ATTEMPT_RE.search(body)
        if match is None or match.group(1) not in active:
            continue
        worker = match.group(1)
        record = fields(body)
        # Durable marker identity is authoritative; inconsistent fields cannot
        # let one worker refresh another worker's timestamp.
        if record.get("worker") != f"opencode-free-model-factory-{worker}":
            continue
        updated = timestamp(record.get("updated", ""))
        if updated is None or updated > now:
            continue
        target = heartbeats if HEARTBEAT_RE.search(body) else attempts
        previous = timestamp(target.get(worker, {}).get("updated", ""))
        if previous is None or updated > previous:
            target[worker] = record

    workers: list[dict[str, object]] = []
    ages: list[int] = []
    for worker in sorted(active, key=int):
        heartbeat = heartbeats.get(worker, {})
        attempt = attempts.get(worker, {})
        updated = timestamp(heartbeat.get("updated", ""))
        age = int((now - updated).total_seconds()) if updated else None
        if age is not None:
            ages.append(age)
        freshness = "missing" if age is None else "stale" if age >= stale_seconds else "fresh"
        outcome = attempt.get("attempt outcome", "")
        quota_limited = outcome in {"provider_throttle", "provider_quota", "provider_throttled"}
        detail = attempt.get("detail", "")
        if re.search(r"(?i)(weekly|week).*(quota|limit)|(quota|limit).*(weekly|week)", detail):
            quota_limited = True
        workers.append({
            "worker": worker,
            "identity": f"opencode-free-model-factory-{worker}",
            "freshness": freshness,
            "age_seconds": age,
            "updated": heartbeat.get("updated", ""),
            "run": heartbeat.get("run", ""),
            "outcome": heartbeat.get("outcome", ""),
            "attempt_outcome": outcome,
            "attempt_updated": attempt.get("updated", ""),
            "attempt_run": attempt.get("run", ""),
            "runtime_status": "quota-limited" if quota_limited else outcome or "unknown",
            "detail": detail,
        })
    return {
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "stale_seconds": stale_seconds,
        "workers": workers,
        "affected_workers": [row["worker"] for row in workers if row["freshness"] != "fresh"],
        "newest_active_age_seconds": min(ages) if ages else None,
        "recovery_policy": "fleet-wide dispatcher recovery only; no per-worker stale retries",
    }


def heartbeat_health_from_view(
    view: Mapping[str, object], roster: Mapping[str, object], *,
    now: datetime, stale_seconds: int = STALE_SECONDS,
) -> dict[str, object]:
    """Read trustworthy repo-scoped lifecycle projections, not comment bodies."""
    expected = {str(worker) for worker in roster.get("expected_workers", [])}
    excluded = {str(worker) for worker in [
        *roster.get("retired_workers", []), *roster.get("paused_workers", [])
    ]}
    heartbeats = view.get("heartbeats") or {}
    attempts = view.get("attempts") or {}
    workers: list[dict[str, object]] = []
    ages: list[int] = []
    for worker in sorted(expected - excluded, key=int):
        heartbeat = heartbeats.get(worker, {})
        attempt = attempts.get(worker, {})
        updated = timestamp(str(heartbeat.get("updated") or ""))
        if updated is not None and updated > now:
            updated = None
        age = int((now - updated).total_seconds()) if updated else None
        if age is not None:
            ages.append(age)
        outcome = str(attempt.get("attempt outcome") or "")
        detail = str(attempt.get("detail") or "")
        quota = outcome in {"provider_throttle", "provider_quota", "provider_throttled"}
        if re.search(r"(?i)(weekly|week).*(quota|limit)|(quota|limit).*(weekly|week)", detail):
            quota = True
        workers.append({
            "worker": worker,
            "identity": f"opencode-free-model-factory-{worker}",
            "freshness": ("missing" if age is None else
                          "stale" if age >= stale_seconds else "fresh"),
            "age_seconds": age,
            "updated": str(heartbeat.get("updated") or "") if updated else "",
            "run": str(heartbeat.get("run") or ""),
            "outcome": str(heartbeat.get("outcome") or ""),
            "attempt_outcome": outcome,
            "attempt_updated": str(attempt.get("updated") or ""),
            "attempt_run": str(attempt.get("run") or ""),
            "runtime_status": "quota-limited" if quota else outcome or "unknown",
            "detail": detail,
        })
    return {
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "stale_seconds": stale_seconds,
        "workers": workers,
        "affected_workers": [row["worker"] for row in workers if row["freshness"] != "fresh"],
        "newest_active_age_seconds": min(ages) if ages else None,
        "recovery_policy": "fleet-wide dispatcher recovery only; no per-worker stale retries",
    }


def markdown_report(report: Mapping[str, object]) -> str:
    """Render freshness and observed runtime outcomes as separate diagnostics."""
    lines = [
        "## Active factory heartbeat visibility",
        "",
        "Stale/missing observations do not imply a crashed worker. Quota exhaustion can be legitimate.",
        "No per-worker retries are triggered by this report.",
        "",
        "| Worker | Freshness | Last heartbeat (UTC) | Last run | Runtime observation |",
        "|---|---|---|---|---|",
    ]
    for row in report["workers"]:
        lines.append(
            f'| {row["identity"]} | {row["freshness"]} | {row["updated"] or "missing"} '
            f'| {row["run"] or row["attempt_run"] or "unknown"} | {row["runtime_status"]} |'
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    """Read trusted registry snapshots and write JSON plus optional job diagnostics."""
    from factory_work_policy import comment_is_trusted

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comments", type=Path)
    parser.add_argument("--view", type=Path)
    parser.add_argument("--roster", type=Path, help="Optional historical test fixture")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    if (args.comments is None) == (args.view is None):
        parser.error("provide exactly one of --comments or --view")
    if args.roster:
        projection = json.loads(args.roster.read_text())
    else:
        from factory_worker_slots import slots

        projection = {
            "expected_workers": [int(slot.worker) for slot in slots()],
            "retired_workers": [],
            "paused_workers": [],
        }
    if args.view:
        report = heartbeat_health_from_view(
            json.loads(args.view.read_text()),
            projection, now=datetime.now(UTC),
        )
    else:
        pages = json.loads(args.comments.read_text())
        comments = [comment for page in pages for comment in page]
        report = heartbeat_health(
            [comment for comment in comments if comment_is_trusted(comment)],
            projection, now=datetime.now(UTC),
        )
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    diagnostics = markdown_report(report)
    print(diagnostics)
    if args.summary:
        with args.summary.open("a") as stream:
            stream.write(diagnostics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
