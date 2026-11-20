#!/usr/bin/env python3
"""Read-only, local ComicPile factory health. Never assigns workers or changes state."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_REPO = "JoshCLWren/comic-pile"
ACTIVE = frozenset({"in_progress", "queued", "pending", "waiting", "requested"})
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SNAPSHOT_MARKER = '<script id="fleet-snapshot" type="application/json">'
FAILURE_ALERT_COUNT = 3
LONG_RUNNING_MINUTES = 30


def instant(value: object) -> datetime | None:
    """Parse timezone-aware GitHub timestamps into UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def gh_json(endpoint: str) -> object:
    """One authenticated read. Never retry in a tight loop or mutate GitHub."""
    try:
        result = subprocess.run(
            ["gh", "api", endpoint], check=True, capture_output=True,
            text=True, timeout=25,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("GitHub CLI (gh) is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub API timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "GitHub API request failed (check gh auth status and rate limits): "
            + (exc.stderr or "").strip()[:180]
        ) from exc
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("GitHub returned invalid JSON") from exc


def public_snapshot(repo: str) -> tuple[dict[str, Any] | None, str | None]:
    """Display-only evidence, never used to infer live worker delivery."""
    request = Request(
        f"https://raw.githubusercontent.com/{repo}/factory-status-live/index.html",
        headers={"User-Agent": "comic-pile-factory-status/1", "Cache-Control": "no-cache"},
    )
    try:
        with urlopen(request, timeout=7) as response:
            content = response.read(2_000_001)
        if len(content) > 2_000_000:
            return None, "snapshot exceeded size limit"
        page = content.decode("utf-8")
        start = page.find(SNAPSHOT_MARKER)
        if start < 0:
            return None, "dashboard snapshot not found"
        start += len(SNAPSHOT_MARKER)
        end = page.find("</script>", start)
        if end < 0:
            return None, "dashboard snapshot is incomplete"
        value = json.loads(page[start:end])
        if not isinstance(value, dict):
            return None, "dashboard snapshot is invalid"
        return value, None
    except (OSError, HTTPError, URLError, ValueError, UnicodeError) as exc:
        return None, str(exc)[:120]


def age_string(now: datetime, date: datetime | None) -> str:
    """Render an observed event age without implying liveness."""
    if date is None:
        return "unknown age"
    minutes = max(0, int((now - date).total_seconds() // 60))
    return f"{minutes}m ago" if minutes < 60 else f"{minutes // 60}h {minutes % 60}m ago"


def recent(row: dict[str, Any], cutoff: datetime, field: str = "created_at") -> bool:
    """Check whether a timestamped GitHub record falls within the selected window."""
    when = instant(row.get(field))
    return when is not None and when >= cutoff


def factory_pr(pr: dict[str, Any]) -> bool:
    """Identify PRs carrying the repository's factory label."""
    return any(
        isinstance(label, dict) and label.get("name") == "factory"
        for label in pr.get("labels") or []
    )


def model_produced_pr(pr: dict[str, Any]) -> bool:
    """Accept both fixed-model attribution and trusted-looking handoff metadata.

    PR descriptions may be rewritten during review. A marker is supporting
    metadata, not proof that the model authored the final reviewed changes.
    """
    if not factory_pr(pr):
        return False
    body = str(pr.get("body") or "")
    return "Produced by fixed-model Factory" in body or (
        "<!-- factory-checkpoint-handoff:v1 " in body
        and "Worker: opencode-free-model-factory-" in body
        and "Model: " in body
    )


def bot_authored_factory_pr(pr: dict[str, Any]) -> bool:
    """Bot ownership is a separate, broader category than model attribution."""
    user = pr.get("user")
    return factory_pr(pr) and isinstance(user, dict) and user.get("login") == "github-actions[bot]"


def workflow_runs(
    fetch: Callable[[str], Any], repo: str, workflow: str, limit: int,
    *, branch: str = "",
) -> list[dict[str, Any]]:
    """Fetch workflow executions through the read-only GitHub API."""
    params = f"branch={branch}&" if branch else ""
    payload = fetch(f"repos/{repo}/actions/workflows/{workflow}/runs?{params}per_page={limit}")
    rows = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"Invalid {workflow} runs response")
    return rows


def snapshot_status(snapshot: dict[str, Any] | None, now: datetime) -> tuple[str, str]:
    """Classify dashboard freshness without affecting live execution counts."""
    if snapshot is None:
        return "UNAVAILABLE", "no dashboard evidence"
    generated = instant(snapshot.get("generated_at"))
    if generated is None or generated > now + timedelta(minutes=2):
        return "UNKNOWN", "missing or invalid generation time"
    age = now - generated
    return ("STALE" if age > timedelta(minutes=30) else "CURRENT"), age_string(now, generated)


def collect(
    fetch: Callable[[str], Any], repo: str, now: datetime, *,
    minutes: int = 60, snapshot: dict[str, Any] | None = None,
    snapshot_error: str | None = None,
) -> dict[str, Any]:
    """Bounded read budget: four GitHub API calls; no per-worker polling."""
    cutoff = now - timedelta(minutes=minutes)
    entries = workflow_runs(fetch, repo, "free-model-factory-entry.yml", 100)
    dispatch = workflow_runs(fetch, repo, "fixed-model-factory-dispatch.yml", 40)
    ci = workflow_runs(fetch, repo, "ci.yml", 10, branch="main")
    prs = fetch(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100")
    if not isinstance(prs, list):
        raise RuntimeError("Invalid pull-request response")

    active_entries = [r for r in entries if r.get("status") == "in_progress"]
    queued_entries = [r for r in entries if r.get("status") in ACTIVE - {"in_progress"}]
    ended_entries = [r for r in entries if r.get("status") == "completed" and recent(r, cutoff)]
    failed_entries = [
        r for r in ended_entries if r.get("conclusion") in {"failure", "timed_out", "cancelled"}
    ]
    successful_entries = [r for r in ended_entries if r.get("conclusion") == "success"]
    long_running = [
        r for r in active_entries
        if (started := instant(r.get("run_started_at") or r.get("created_at")))
        and now - started >= timedelta(minutes=LONG_RUNNING_MINUTES)
    ]
    launched_dispatches = [r for r in dispatch if recent(r, cutoff)]
    opened = [pr for pr in prs if factory_pr(pr) and recent(pr, cutoff)]
    merged = [pr for pr in prs if factory_pr(pr) and recent(pr, cutoff, "merged_at")]
    created_by_model = [pr for pr in opened if model_produced_pr(pr)]
    merged_from_model = [pr for pr in merged if model_produced_pr(pr)]
    bot_opened = [pr for pr in opened if bot_authored_factory_pr(pr)]
    bot_merged = [pr for pr in merged if bot_authored_factory_pr(pr)]
    # When the newest 40 dispatch runs all fall inside the window, the
    # reported count is a lower bound, not the total dispatch rate.
    dispatch_capped = len(dispatch) == 40 and bool(launched_dispatches) and recent(dispatch[-1], cutoff)
    main_ci = next((r for r in ci if r.get("head_branch") == "main"), None)
    last_dispatch = next(iter(dispatch), None)
    freshness, elapsed = snapshot_status(snapshot, now)

    # A successful Actions job proves execution, not persisted or deployed code.
    if len(failed_entries) >= FAILURE_ALERT_COUNT and not successful_entries:
        # A recently merged PR may have been opened days earlier or repaired by
        # another contributor. Repeated failing Entry runs outrank merge credit.
        verdict = "DEGRADED: REPEATED FAILED ENTRIES; CURRENT DELIVERY UNPROVEN"
    elif merged_from_model:
        verdict = "FACTORY-ATTRIBUTED PR MERGED (production not verified)"
    elif bot_merged:
        verdict = "FACTORY BOT PR MERGED; MODEL ATTRIBUTION UNVERIFIED"
    elif created_by_model:
        verdict = "FACTORY-ATTRIBUTED PR OPENED (merge/deploy unverified)"
    elif active_entries:
        verdict = "WORKERS RUNNING; DELIVERY NOT PROVEN"
    elif queued_entries:
        verdict = "WORKERS QUEUED; DELIVERY NOT PROVEN"
    elif ended_entries and all(r.get("conclusion") != "success" for r in ended_entries):
        verdict = "NO RECENT SUCCESSFUL ENTRIES; INVESTIGATE"
    else:
        verdict = "NO RECENT FACTORY DELIVERY PROVEN"

    return {
        "repo": repo, "now": now.isoformat(), "minutes": minutes,
        "verdict": verdict, "active_entries": active_entries,
        "queued_entries": queued_entries, "ended_entries": ended_entries,
        "failed_entries": failed_entries, "successful_entries": successful_entries,
        "long_running": long_running,
        "dispatches": launched_dispatches, "last_dispatch": last_dispatch,
        "opened": opened, "merged": merged, "model_opened": created_by_model,
        "model_merged": merged_from_model,
        "bot_opened": bot_opened, "bot_merged": bot_merged,
        "dispatch_capped": dispatch_capped, "main_ci": main_ci,
        "snapshot_freshness": freshness, "snapshot_age": elapsed,
        "snapshot_error": snapshot_error, "pr_window_limited": len(prs) == 100,
    }


def render(data: dict[str, Any]) -> str:
    """Render a console-readable status report with evidence links."""
    def workflow_link(row: dict[str, Any] | None) -> str:
        return str((row or {}).get("html_url") or "not available")

    def pr_link(row: dict[str, Any]) -> str:
        return str(row.get("html_url") or f"https://github.com/{data['repo']}/pull/{row.get('number')}")

    ended = data["ended_entries"]
    successes = sum(r.get("conclusion") == "success" for r in ended)
    failures = sum(r.get("conclusion") in ("failure", "timed_out", "cancelled") for r in ended)
    lines = [
        f"ComicPile factory | last {data['minutes']}m | "
        f"{datetime.fromisoformat(data['now']).astimezone():%Y-%m-%d %H:%M %Z}",
        f"VERDICT: {data['verdict']}",
        "",
        f"Workers: {len(data['active_entries'])} running, {len(data['queued_entries'])} queued; "
        f"{successes} successful / {failures} failed recent entry runs",
        f"Long-running workers: {len(data['long_running'])} running for at least "
        f"{LONG_RUNNING_MINUTES}m (not necessarily hung)",
        f"Dispatcher: {'at least ' if data['dispatch_capped'] else ''}{len(data['dispatches'])} "
        f"run(s) started; last "
        f"{(data['last_dispatch'] or {}).get('conclusion') or (data['last_dispatch'] or {}).get('status') or 'unknown'}",
        f"Factory PRs: {len(data['opened'])} opened, {len(data['merged'])} merged "
        "(last 100 updated PRs; local and remote contributors included)",
        f"Checkpoint-attributed PRs: {len(data['model_opened'])} created, {len(data['model_merged'])} merged "
        "(metadata evidence, not independent code/deploy validation)",
        f"Bot-authored factory PRs: {len(data['bot_opened'])} created, {len(data['bot_merged'])} merged "
        "(may include human-authored fixes)",
        f"Main CI: {(data['main_ci'] or {}).get('conclusion') or (data['main_ci'] or {}).get('status') or 'unknown'}",
        f"Dashboard: {data['snapshot_freshness']} ({data['snapshot_age']})",
    ]
    if data["pr_window_limited"]:
        lines.append("NOTE: PR list capped at 100, counts may understate activity.")
    if data["dispatch_capped"]:
        lines.append("NOTE: Dispatcher list capped at 40; actual dispatch count may be higher.")
    if data["snapshot_error"]:
        lines.append("NOTE: Dashboard snapshot unavailable; live GitHub data remains available.")
    lines.extend(["", "Evidence:"])
    for run in data["active_entries"][:4]:
        started = instant(run.get("run_started_at") or run.get("created_at"))
        lines.append(
            f"  RUNNING  {run.get('name') or run.get('id')} "
            f"({age_string(datetime.fromisoformat(data['now']), started)})  {workflow_link(run)}"
        )
    for run in data["failed_entries"][:4]:
        lines.append(f"  FAILED   {run.get('name') or run.get('id')}  {workflow_link(run)}")
    for pr in data["model_opened"][:3]:
        lines.append(f"  CODE PR  #{pr.get('number')}  {pr_link(pr)}")
    for pr in data["model_merged"][:3]:
        lines.append(f"  MERGED   #{pr.get('number')}  {pr_link(pr)}")
    for pr in [p for p in data["bot_merged"] if p not in data["model_merged"]][:3]:
        lines.append(f"  BOT PR   #{pr.get('number')}  {pr_link(pr)}")
    if data["last_dispatch"]:
        lines.append(f"  DISPATCH {workflow_link(data['last_dispatch'])}")
    if data["main_ci"]:
        lines.append(f"  MAIN CI  {workflow_link(data['main_ci'])}")
    lines.extend([
        "",
        "A green Actions run proves execution, not persisted code. "
        "End-to-end health requires a real factory PR, merge, green main CI, and deploy.",
    ])
    return "\n".join(lines)


def main() -> int:
    """Run a bounded, read-only status probe or interval watch."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="Refresh periodically (120s minimum)")
    parser.add_argument("--no-dashboard", action="store_true", help="Skip public dashboard freshness probe")
    args = parser.parse_args()
    if not REPO_RE.fullmatch(args.repo) or not 1 <= args.minutes <= 1440:
        parser.error("valid owner/repository and a window of 1-1440 minutes required")
    if args.watch is not None and args.watch < 120:
        parser.error("--watch must be at least 120 seconds to protect API budget")
    while True:
        try:
            snapshot, error = (None, "disabled") if args.no_dashboard else public_snapshot(args.repo)
            report = collect(
                gh_json, args.repo, datetime.now(UTC),
                minutes=args.minutes, snapshot=snapshot, snapshot_error=error,
            )
        except RuntimeError as exc:
            print(f"UNKNOWN: {exc}\nDo not infer factory health from a failed status check.", file=sys.stderr)
            return 2
        if args.watch:
            print("\033[2J\033[H", end="")
        print(render(report), flush=True)
        if args.watch is None:
            return 0
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
