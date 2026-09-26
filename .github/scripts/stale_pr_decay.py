#!/usr/bin/env python3
"""Adaptive stale-PR decay guard for autonomous Factory implementation attempts.

Factory PRs converge quickly; long-lived attempts indicate failed convergence.
This module calculates staleness relative to a rolling population of the last
100 eligible successfully merged Factory PRs, using a robust percentile-based
threshold (default 2x P90) with a documented lower floor.

Staleness is an attempt-level circuit breaker, independent of semantic strikes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import time
from datetime import datetime
from typing import Any, cast

REPO = os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
STALE_PR_GUARD_MARKER_RE = re.compile(
    r"comic-pile-factory-stale-expiration-v1:pr-(?P<pr>\d+):issue-(?P<issue>\d+):"
    r"age-(?P<age>\d+):sample-(?P<sample>\d+):"
    r"formula-(?P<formula>[^:]+):threshold-(?P<threshold>\d+)"
)
STALE_MARKER = "comic-pile-factory-stale-expiration-v1"
IMPLEMENT_CLAIM_RE = re.compile(
    r"comic-pile-factory-implement-claim-v3:issue-(?P<issue>\d+):"
    r"opencode-(?:free-model|nvidia|omniroute)-factory-(?P<worker>\d+):"
)
FACTORY_LABEL = "factory"
FACTORY_BRANCH_RE = re.compile(r"^factory/\d+-(\d+)-")

DEFAULT_ROLLING_WINDOW = 100
DEFAULT_PERCENTILE_MULTIPLIER = 2.0
DEFAULT_P90_MULTIPLE = 2.0
MINIMUM_SAMPLE_SIZE = 10
LOWER_FLOOR_SECONDS = 3600
COLD_START_FALLBACK_SECONDS = 86400
STALE_EXPIRY_RESET_SECONDS = 86400


def parse_iso_epoch(value: str | None) -> int | None:
    """Parse an ISO timestamp into epoch seconds, or None."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def is_factory_implementation_pr(pr: dict[str, Any]) -> bool:
    """Return whether a PR is an autonomous Factory implementation attempt.

    Only PRs on canonical factory branches or carrying the factory label
    (excluding infrastructure) qualify. Dependabot, manual/Codex work,
    and non-product automation are excluded.
    """
    head = str(pr.get("headRefName") or "")
    labels = {
        label.get("name") if isinstance(label, dict) else label
        for label in (pr.get("labels") or [])
    }
    if "infrastructure" in labels:
        return False
    if head.startswith("factory/"):
        return True
    if "factory" in labels:
        return True
    return False


def is_successfully_merged_factory_pr(pr: dict[str, Any]) -> bool:
    """Return whether a PR is a successfully merged Factory implementation."""
    if str(pr.get("state") or "").upper() != "MERGED":
        return False
    if not is_factory_implementation_pr(pr):
        return False
    return True


def pr_lifetime_seconds(pr: dict[str, Any], now_epoch: int | None = None) -> int | None:
    """Calculate PR lifetime in seconds.

    For merged PRs: merged_at - created_at.
    For open PRs: now - created_at.
    Uses created_at, never updated_at.
    """
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    created = parse_iso_epoch(str(pr.get("createdAt") or ""))
    if created is None:
        return None
    merged_at = parse_iso_epoch(str(pr.get("mergedAt") or ""))
    if merged_at is not None:
        return max(0, merged_at - created)
    return max(0, now_epoch - created)


def get_eligible_merged_prs(
    prs: list[dict[str, Any]],
    now_epoch: int | None = None,
    limit: int = DEFAULT_ROLLING_WINDOW,
) -> list[dict[str, Any]]:
    """Return the last N eligible successfully merged Factory PRs, sorted by merge time descending."""
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    eligible = [
        pr for pr in prs
        if is_successfully_merged_factory_pr(pr)
    ]
    eligible.sort(key=lambda p: parse_iso_epoch(str(p.get("mergedAt") or "")) or 0, reverse=True)
    return eligible[:limit]


def calculate_percentile(values: list[float], percentile: float) -> float:
    """Calculate a percentile value from a sorted list of values using linear interpolation."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (percentile / 100.0) * (len(sorted_values) - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def calculate_mad_threshold(
    lifetimes: list[float],
    multiplier: float = DEFAULT_P90_MULTIPLE,
) -> float:
    """Calculate a robust median/MAD-based threshold.

    Median + multiplier * MAD resists a handful of unusually old successful PRs
    stretching the threshold indefinitely.
    """
    if not lifetimes:
        return 0.0
    median = statistics.median(lifetimes)
    deviations = [abs(v - median) for v in lifetimes]
    mad = statistics.median(deviations) if deviations else 0.0
    return median + multiplier * mad


def calculate_percentile_threshold(
    lifetimes: list[float],
    p90_multiplier: float = DEFAULT_P90_MULTIPLE,
) -> float:
    """Calculate the 2x P90 percentile-based threshold."""
    p90 = calculate_percentile(lifetimes, 90)
    return p90 * p90_multiplier


def calculate_staleness_threshold(
    lifetimes: list[float],
    multiplier: float = DEFAULT_PERCENTILE_MULTIPLIER,
) -> float:
    """Calculate the staleness threshold from the rolling population.

    Uses 2x P90 as the default candidate, compared with median/MAD.
    Returns the more conservative (larger) of the two to avoid euthanizing
    healthy young PRs, subject to a documented lower floor.
    """
    if len(lifetimes) < MINIMUM_SAMPLE_SIZE:
        return COLD_START_FALLBACK_SECONDS

    p90_threshold = calculate_percentile_threshold(lifetimes)
    mad_threshold = calculate_mad_threshold(lifetimes)

    threshold = max(p90_threshold, mad_threshold)
    threshold = max(threshold, LOWER_FLOOR_SECONDS)
    return threshold


def get_staleness_baseline(
    prs: list[dict[str, Any]],
    now_epoch: int | None = None,
    limit: int = DEFAULT_ROLLING_WINDOW,
) -> dict[str, Any]:
    """Build the staleness baseline from recent successful merged Factory PRs.

    Returns a dict with sample size, threshold, statistics, and all lifetimes.
    """
    eligible = get_eligible_merged_prs(prs, now_epoch=now_epoch, limit=limit)
    lifetimes = []
    for pr in eligible:
        lifetime = pr_lifetime_seconds(pr, now_epoch=now_epoch)
        if lifetime is not None:
            lifetimes.append(float(lifetime))

    if not lifetimes:
        return {
            "sample_size": 0,
            "threshold_seconds": COLD_START_FALLBACK_SECONDS,
            "formula": "cold-start-fallback",
            "p90": None,
            "median": None,
            "mad": None,
            "lower_floor": LOWER_FLOOR_SECONDS,
            "lifetimes_seconds": [],
        }

    threshold = calculate_staleness_threshold(lifetimes)
    p90 = calculate_percentile(lifetimes, 90)
    median = statistics.median(lifetimes)
    mad = statistics.median([abs(v - median) for v in lifetimes])

    formula = "2x_p90_vs_mad_max"
    if threshold <= LOWER_FLOOR_SECONDS:
        formula = "lower_floor"
    elif len(lifetimes) < MINIMUM_SAMPLE_SIZE:
        formula = "cold-start-fallback"

    return {
        "sample_size": len(lifetimes),
        "threshold_seconds": threshold,
        "formula": formula,
        "p90": p90,
        "median": median,
        "mad": mad,
        "lower_floor": LOWER_FLOOR_SECONDS,
        "lifetimes_seconds": lifetimes,
    }


def is_stale_attempt(
    pr_age_seconds: float,
    baseline: dict[str, Any],
) -> bool:
    """Return whether a PR implementation attempt is stale."""
    threshold = baseline["threshold_seconds"]
    return pr_age_seconds > threshold


def build_stale_expiration_marker(
    pr_number: int,
    issue_number: int,
    age_seconds: float,
    baseline: dict[str, Any],
) -> str:
    """Build a durable stale-expiration marker comment."""
    return (
        f"<!-- {STALE_MARKER}:pr-{pr_number}:issue-{issue_number}:"
        f"age-{int(age_seconds)}:sample-{baseline['sample_size']}:"
        f"formula-{baseline['formula']}:threshold-{int(baseline['threshold_seconds'])} -->"
    )


def parse_stale_marker(body: str) -> dict[str, Any] | None:
    """Parse a stale-expiration marker from a comment body."""
    match = STALE_PR_GUARD_MARKER_RE.search(body)
    if not match:
        return None
    return {
        "pr": int(match.group("pr")),
        "issue": int(match.group("issue")),
        "age": int(match.group("age")),
        "sample": int(match.group("sample")),
        "formula": match.group("formula"),
        "threshold": int(match.group("threshold")),
    }


def has_stale_expiration_marker(
    comments: list[dict[str, Any]],
    pr_number: int,
) -> bool:
    """Return whether a PR already has a trusted stale-expiration marker."""
    for comment in comments:
        # Debug: print the type and structure of comment
        if not isinstance(comment, dict):
            continue
        if not comment_is_trusted(comment):
            continue
        body = str(comment.get("body") or "")
        marker = parse_stale_marker(body)
        if marker and marker["pr"] == pr_number:
            return True
    return False


def comment_is_trusted(comment: dict[str, Any]) -> bool:
    """Return whether GitHub metadata proves a factory marker is trusted."""
    trusted = {"OWNER", "MEMBER", "COLLABORATOR"}
    if comment.get("author_association") in trusted:
        return True
    app = comment.get("performed_via_github_app")
    return isinstance(app, dict) and app.get("slug") == "github-actions"


def gh_json(args: list[str], *, input_json: object | None = None) -> object | None:
    """Run GitHub CLI and decode JSON stdout."""
    output = run_gh(args, input_json=input_json)
    return json.loads(output) if output.strip() else None


def run_gh(args: list[str], *, input_json: object | None = None) -> str:
    """Run a bounded GitHub CLI command and return stdout."""
    command = ["gh", *args]
    try:
        proc = subprocess.run(command, input=None if input_json is None else json.dumps(input_json),
                              text=True, capture_output=True, check=False, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(command)} timed out") from exc
    if proc.returncode:
        raise RuntimeError(f"{' '.join(command)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def list_factory_prs(now_epoch: int | None = None) -> list[dict[str, Any]]:
    """List all PRs relevant to staleness calculation."""
    return cast(list[dict[str, Any]], gh_json([
        "pr", "list", "--repo", REPO, "--state", "all", "--limit", "500",
        "--json", "number,title,body,labels,headRefName,createdAt,mergedAt,state"
    ]))


def get_pr_comments(pr_number: int) -> list[dict[str, Any]]:
    """Fetch comments on a PR."""
    return cast(list[dict[str, Any]], gh_json([
        "api", "--paginate", "--slurp",
        f"repos/{REPO}/issues/{pr_number}/comments?per_page=100"
    ]))


def get_pr_payload(pr_number: int) -> dict[str, Any] | None:
    """Fetch a single PR payload."""
    return cast(dict[str, Any] | None, gh_json([
        "api", f"repos/{REPO}/pulls/{pr_number}",
        "--json", "number,title,body,labels,headRefName,createdAt,mergedAt,state"
    ]))


def issue_has_open_blocker(number: int) -> bool:
    """Return whether an issue has an open dependency blocker."""
    try:
        blockers = cast(list[dict[str, Any]], gh_json([
            "api", f"repos/{REPO}/issues/{number}/dependencies/blocked_by?per_page=100"
        ]) or [])
    except RuntimeError:
        return True
    return any(item.get("state") == "open" for item in blockers)


def close_pr_implementation(pr_number: int) -> None:
    """Close a stale PR implementation attempt."""
    run_gh(["pr", "close", str(pr_number), "--repo", REPO])


def reset_issue_to_unowned(number: int) -> None:
    """Return an open linked issue to executable implementation intake."""
    target = gh_json(["api", f"repos/{REPO}/issues/{number}"])
    if not isinstance(target, dict) or str(target.get("state") or "").lower() != "open":
        return
    replace_factory_labels(number, "factory:unowned", "factory:building")


def replace_factory_labels(number: int, owner: str, stage: str | None = None) -> None:
    """Atomically reconcile one target to exactly one owner and stage."""
    target = gh_json(["api", f"repos/{REPO}/issues/{number}"])
    if target is None:
        return
    current = [label["name"] for label in target.get("labels", [])]
    existing_stage = next((label for label in current if label in STAGE_LABELS), None)
    stage = stage or existing_stage or "factory:building"
    labels = [label for label in current if not OWNER_RE.fullmatch(label) and label not in STAGE_LABELS and label != FACTORY_LABEL]
    labels.extend([FACTORY_LABEL, owner, stage])
    run_gh(["api", "--method", "PUT", f"repos/{REPO}/issues/{number}/labels", "--input", "-"],
           input_json={"labels": sorted(set(labels))})


OWNER_RE = re.compile("^factory:(?:unowned|local|[1-9]|[1-3][0-9]|[4-7][0-9])$")
STAGE_LABELS = {'factory:building', 'factory:review', 'factory:changes-requested', 'factory:ci', 'factory:ready', 'factory:blocked'}


def record_stale_expiration(
    pr_number: int,
    issue_number: int,
    age_seconds: float,
    baseline: dict[str, Any],
) -> str:
    """Reset the linked issue, close the stale PR, then record expiration.

    The issue reset happens before PR close so a partial failure remains safe:
    an open canonical PR still suppresses duplicate issue intake, while a
    successful close cannot strand its issue behind a stale factory owner.
    The durable marker is written last so it never suppresses a retry of an
    incomplete expiration.
    """
    marker = build_stale_expiration_marker(pr_number, issue_number, age_seconds, baseline)
    if issue_number:
        reset_issue_to_unowned(issue_number)
    close_pr_implementation(pr_number)
    run_gh(["issue", "comment", str(pr_number), "--repo", REPO, "--body", marker])
    return marker


def process_stale_prs(
    prs: list[dict[str, Any]] | None = None,
    now_epoch: int | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Evaluate open PRs for staleness and expire any that cross the threshold.

    Returns a list of expiration results with details for observability.
    """
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    if prs is None:
        prs = list_factory_prs(now_epoch=now_epoch)

    open_prs = [pr for pr in prs if str(pr.get("state") or "").upper() == "OPEN"]
    baseline = get_staleness_baseline(prs, now_epoch=now_epoch)
    results: list[dict[str, Any]] = []

    for pr in open_prs:
        if not is_factory_implementation_pr(pr):
            continue
        pr_number = int(pr["number"])
        age = pr_lifetime_seconds(pr, now_epoch=now_epoch)
        if age is None:
            continue

        if has_stale_expiration_marker(get_pr_comments(pr_number), pr_number):
            results.append({"pr": pr_number, "status": "already-expired", "age_seconds": age})
            continue

        if not is_stale_attempt(age, baseline):
            results.append({"pr": pr_number, "status": "not-stale", "age_seconds": age})
            continue

        linked_issue = linked_issue_from_pr(pr)
        if dry_run:
            results.append({
                "pr": pr_number,
                "status": "would-expire",
                "age_seconds": age,
                "issue": linked_issue,
                "baseline": baseline,
            })
            continue

        try:
            marker = record_stale_expiration(pr_number, linked_issue or 0, age, baseline)
            results.append({
                "pr": pr_number,
                "status": "expired",
                "age_seconds": age,
                "issue": linked_issue,
                "marker": marker,
            })
        except Exception as exc:
            results.append({"pr": pr_number, "status": "error", "error": str(exc)})

    return results


def linked_issue_from_pr(pr: dict[str, Any]) -> int | None:
    """Extract the issue number linked to a PR."""
    head = str(pr.get("headRefName") or "")
    match = FACTORY_BRANCH_RE.match(head)
    if match:
        return int(match.group(1))
    body = str(pr.get("body") or "")
    closing = re.search(r"(?:closes|closed|close|fixes|fixed|fix|resolves|resolved|resolve)\s+#(\d+)", body, re.IGNORECASE)
    if closing:
        return int(closing.group(1))
    return None


def get_staleness_observability(baseline: dict[str, Any], now_epoch: int | None = None) -> str:
    """Return an observability summary string for logging."""
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    lines = [
        "Stale-PR guard observability:",
        f"  Sample size: {baseline['sample_size']}",
        f"  Threshold: {baseline['threshold_seconds']}s ({baseline['threshold_seconds']/3600:.1f}h)",
        f"  Formula: {baseline['formula']}",
    ]
    if baseline.get("p90") is not None:
        lines.append(f"  P90 lifetime: {baseline['p90']/3600:.1f}h")
    if baseline.get("median") is not None:
        lines.append(f"  Median lifetime: {baseline['median']/3600:.1f}h")
    if baseline.get("lower_floor") is not None:
        lines.append(f"  Lower floor: {baseline['lower_floor']/3600:.1f}h")
    return "\n".join(lines)


class StalePRGuard:
    """Adaptive stale-PR decay guard for Factory implementation attempts."""

    def __init__(
        self,
        rolling_window: int = DEFAULT_ROLLING_WINDOW,
        multiplier: float = DEFAULT_PERCENTILE_MULTIPLIER,
        p90_multiple: float = DEFAULT_P90_MULTIPLE,
        min_sample: int = MINIMUM_SAMPLE_SIZE,
        lower_floor: int = LOWER_FLOOR_SECONDS,
        cold_start_fallback: int = COLD_START_FALLBACK_SECONDS,
    ) -> None:
        self.rolling_window = rolling_window
        self.multiplier = multiplier
        self.p90_multiple = p90_multiple
        self.min_sample = min_sample
        self.lower_floor = lower_floor
        self.cold_start_fallback = cold_start_fallback

    def evaluate_pr(self, pr: dict[str, Any], now_epoch: int | None = None, prs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Evaluate a single PR for staleness.

        Returns a dict with staleness status, age, threshold, and baseline.
        """
        now_epoch = int(time.time()) if now_epoch is None else now_epoch
        pr_number = int(pr["number"])
        age = pr_lifetime_seconds(pr, now_epoch=now_epoch)
        if age is None:
            return {"pr": pr_number, "stale": False, "reason": "unknown_age"}

        all_prs = list(prs) if prs is not None else []
        baseline = self.get_baseline(all_prs, now_epoch=now_epoch)
        stale = is_stale_attempt(age, baseline)

        return {
            "pr": pr_number,
            "stale": stale,
            "age_seconds": age,
            "threshold_seconds": baseline["threshold_seconds"],
            "baseline": baseline,
            "reason": "stale" if stale else "within-threshold",
        }

    def get_baseline(
        self,
        prs: list[dict[str, Any]],
        now_epoch: int | None = None,
    ) -> dict[str, Any]:
        """Get the staleness baseline from the rolling population."""
        return get_staleness_baseline(prs, now_epoch=now_epoch, limit=self.rolling_window)

    def process_stale_prs(
        self,
        prs: list[dict[str, Any]] | None = None,
        now_epoch: int | None = None,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Process all open PRs for staleness expiration."""
        return process_stale_prs(prs=prs, now_epoch=now_epoch, dry_run=dry_run)


def main() -> int:
    """Run the stale-PR decay guard command-line interface."""
    parser = argparse.ArgumentParser(description="Adaptive stale-PR decay guard")
    parser.add_argument("--evaluate", type=int, help="Evaluate a specific PR by number")
    parser.add_argument("--process", action="store_true", help="Process all stale PRs")
    parser.add_argument("--dry-run", action="store_true", help="Report without taking action")
    parser.add_argument("--observability", action="store_true", help="Show observability summary")
    parser.add_argument("--baseline", action="store_true", help="Show staleness baseline")
    parser.add_argument("--limit", type=int, default=DEFAULT_ROLLING_WINDOW, help="Rolling window size")
    args = parser.parse_args()

    if args.observability or args.baseline:
        prs = list_factory_prs()
        baseline = get_staleness_baseline(prs, limit=args.limit)
        if args.observability:
            print(get_staleness_observability(baseline))
        else:
            print(json.dumps(baseline, indent=2, default=str))
        return 0

    if args.evaluate is not None:
        prs = list_factory_prs()
        pr = next((p for p in prs if int(p["number"]) == args.evaluate), None)
        if pr is None:
            print(f"PR #{args.evaluate} not found")
            return 1
        guard = StalePRGuard(rolling_window=args.limit)
        result = guard.evaluate_pr(pr)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.process:
        results = process_stale_prs(dry_run=args.dry_run)
        for r in results:
            print(json.dumps(r, default=str))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())