#!/usr/bin/env python3
"""Generate a human-readable GitHub Pages dashboard for the ComicPile factory."""

from __future__ import annotations

import html
import importlib.util
import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
REPO_URL = f"https://github.com/{REPO}"
OUTPUT = Path(os.environ.get("FACTORY_DASHBOARD_OUTPUT", "site/index.html"))
SCRIPTS = Path(__file__).resolve().parent
MANIFEST = Path(__file__).resolve().parents[1] / "free-model-factories.tsv"
OWNER_RE = re.compile(r"^factory:(?P<worker>\d+)$")
WORKER_RE = re.compile(r"^opencode-free-model-factory-(?P<worker>\d+)$")
ATTEMPT_MARKER = "<!-- factory-attempt-outcome:v1 "
HEARTBEAT_MARKER = "<!-- factory-heartbeat:v1 "
STAGES = (
    "factory:building",
    "factory:changes-requested",
    "factory:review",
    "factory:ci",
    "factory:ready",
    "factory:conflict",
    "factory:conflicted",
    "factory:unowned",
)
ATTENTION_OUTCOMES = {
    "provider_failure",
    "provider_throttle",
    "provider_unavailable",
    "provider_throttled",
    "model_unavailable",
    "model_retired_410",
    "model_policy_violation",
    "environment_failure",
    "control_plane_failure",
    "unknown_failure",
    "worker_environment_failure",
    "model_interruption",
}


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def label_names(item: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for label in item.get("labels") or []:
        name = label.get("name") if isinstance(label, Mapping) else label
        if name:
            names.add(str(name))
    return names


def count_with_label(items: Iterable[Mapping[str, Any]], label: str) -> int:
    return sum(label in label_names(item) for item in items)


def github_search_total(controller: Any, query: str) -> int:
    result = controller.gh_json(
        ["api", "-X", "GET", "search/issues", "-f", f"q=repo:{REPO} {query}", "-f", "per_page=1"]
    )
    return int(result.get("total_count") or 0) if isinstance(result, Mapping) else 0


def iso_search_time(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_completion_funnel(comments: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    marker = "<!-- factory-completion-funnel:v1 -->"
    for comment in reversed(tuple(comments)):
        body = str(comment.get("body") or "")
        if marker not in body:
            continue
        values: dict[str, str] = {}
        for line in body.splitlines():
            if ":" in line and not line.startswith("<!--"):
                key, value = line.split(":", 1)
                values[key.strip().casefold()] = value.strip()
        return values
    return {}


def load_manifest_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in raw_line.split("\t")]
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        fields += [""] * (6 - len(fields))
        worker, source, model, minute, scheduler, display_name = fields[:6]
        rows.append(
            {
                "worker": worker,
                "source": source,
                "model": model,
                "minute": minute,
                "scheduler": scheduler,
                "display_name": display_name or model,
            }
        )
    return rows


def parse_registry_record(body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line or line.startswith("<!--"):
            continue
        key, value = line.split(":", 1)
        values[key.strip().casefold()] = value.strip()
    match = WORKER_RE.fullmatch(values.get("worker", ""))
    if match:
        values["worker_id"] = match.group("worker")
    return values


def latest_worker_records(
    comments: Iterable[Mapping[str, Any]],
    *,
    trusted: Callable[[Mapping[str, Any]], bool] | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    attempts: dict[str, dict[str, str]] = {}
    heartbeats: dict[str, dict[str, str]] = {}
    for comment in comments:
        if trusted and not trusted(comment):
            continue
        body = str(comment.get("body") or "")
        if ATTEMPT_MARKER not in body and HEARTBEAT_MARKER not in body:
            continue
        record = parse_registry_record(body)
        worker = record.get("worker_id")
        updated = record.get("updated")
        if not worker or not updated:
            continue
        target = attempts if ATTEMPT_MARKER in body else heartbeats
        if updated > target.get(worker, {}).get("updated", ""):
            target[worker] = record
    return attempts, heartbeats


def owner_worker(item: Mapping[str, Any]) -> str | None:
    for name in label_names(item):
        match = OWNER_RE.fullmatch(name)
        if match:
            return match.group("worker")
    return None


def current_assignments(
    issues: Iterable[Mapping[str, Any]], prs: Iterable[Mapping[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for kind, items in (("issue", issues), ("pr", prs)):
        for item in items:
            worker = owner_worker(item)
            if not worker:
                continue
            number = int(item.get("number") or 0)
            labels = label_names(item)
            stage = next(
                (label.removeprefix("factory:") for label in STAGES if label in labels),
                "",
            )
            result[worker].append(
                {
                    "kind": kind,
                    "number": number,
                    "title": str(item.get("title") or ""),
                    "url": str(
                        item.get("url")
                        or item.get("html_url")
                        or (
                            f"{REPO_URL}/pull/{number}"
                            if kind == "pr"
                            else f"{REPO_URL}/issues/{number}"
                        )
                    ),
                    "stage": stage,
                }
            )
    return dict(result)


def recently_merged_prs(controller: Any, *, days: int = 7) -> list[dict[str, Any]]:
    result = controller.gh_json(
        [
            "pr",
            "list",
            "--repo",
            REPO,
            "--state",
            "merged",
            "--search",
            f"merged:>={iso_search_time(timedelta(days=days))}",
            "--limit",
            "1000",
            "--json",
            "number,title,url,mergedAt,labels",
        ]
    )
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


def merge_credit(merged_prs: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    now = datetime.now(timezone.utc)
    credits: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"day": 0, "week": 0, "last_merge": ""}
    )
    for pr in merged_prs:
        worker = owner_worker(pr)
        merged_at = str(pr.get("mergedAt") or pr.get("merged_at") or "")
        if not worker:
            continue
        try:
            merged = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        credits[worker]["week"] += 1
        credits[worker]["day"] += int(now - merged <= timedelta(hours=24))
        credits[worker]["last_merge"] = max(str(credits[worker]["last_merge"]), merged_at)
    return dict(credits)


def quality_tier(row: Mapping[str, Any]) -> str:
    health = str(row.get("health") or "unknown")
    outcome = str(row.get("attempt_outcome") or "")
    week = int(row.get("merged_week") or 0)
    if health == "unavailable":
        return "blocked"
    if health in {"cooling", "degraded"} or outcome in ATTENTION_OUTCOMES:
        return "watch"
    if week >= 2 and health == "healthy":
        return "proven"
    if week:
        return "productive"
    if row.get("activity") == "working":
        return "working"
    if health == "healthy" and outcome in {"success", "no_work"}:
        return "healthy"
    return "unproven"


def build_worker_rows(
    manifest_rows: Iterable[Mapping[str, str]],
    *,
    capacity_rows: Iterable[Mapping[str, Any]],
    assignments: Mapping[str, list[dict[str, Any]]],
    attempts: Mapping[str, Mapping[str, str]],
    heartbeats: Mapping[str, Mapping[str, str]],
    credits: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    health = {
        str(item.get("worker") or ""): str(item.get("health") or "unknown")
        for item in capacity_rows
        if isinstance(item, Mapping)
    }
    rows: list[dict[str, Any]] = []
    for manifest in manifest_rows:
        worker = str(manifest["worker"])
        attempt = attempts.get(worker, {})
        heartbeat = heartbeats.get(worker, {})
        credit = credits.get(worker, {})
        work = list(assignments.get(worker, []))
        row: dict[str, Any] = {
            "worker": worker,
            "display_name": str(manifest.get("display_name") or manifest.get("model") or ""),
            "runtime_source": str(attempt.get("source") or manifest.get("source") or ""),
            "runtime_model": str(attempt.get("model") or manifest.get("model") or ""),
            "health": health.get(worker, "unknown"),
            "activity": "working"
            if work or heartbeat.get("outcome") == "running"
            else "idle",
            "attempt_outcome": str(attempt.get("attempt outcome") or ""),
            "attempt_detail": str(attempt.get("detail") or ""),
            "attempt_run": str(attempt.get("run") or ""),
            "updated": max(
                str(attempt.get("updated") or ""), str(heartbeat.get("updated") or "")
            ),
            "merged_day": int(credit.get("day") or 0),
            "merged_week": int(credit.get("week") or 0),
            "work": work,
        }
        row["tier"] = quality_tier(row)
        rows.append(row)
    rank = {
        "proven": 0,
        "productive": 1,
        "working": 2,
        "healthy": 3,
        "unproven": 4,
        "watch": 5,
        "blocked": 6,
    }
    return sorted(
        rows,
        key=lambda row: (
            rank.get(str(row["tier"]), 9),
            -int(row["merged_week"]),
            int(row["worker"]),
        ),
    )


def system_verdict(snapshot: Mapping[str, Any]) -> tuple[str, str, str]:
    demand = int(snapshot["completion_demand"]) + int(snapshot["production_demand"])
    executable = int(snapshot["executable_slot_capacity"])
    busy = int(snapshot["busy_workers"])
    throughput = snapshot["throughput"]
    if demand == 0:
        return "idle", "Idle", "No eligible factory work is waiting."
    if executable == 0:
        return "bad", "Blocked", f"{demand} eligible items exist, but no slot is executable."
    if busy:
        direction = (
            "the 24h PR backlog is shrinking"
            if int(throughput["net_day"]) < 0
            else "the 24h PR backlog is not shrinking"
        )
        return "good", "Working", f"{busy} factories hold live work; {direction}."
    if int(throughput["merged_hour"]) > 0:
        return "good", "Working", f"{throughput['merged_hour']} PRs merged in the last hour."
    return (
        "warn",
        "Attention",
        f"{demand} eligible items and {executable} executable slots exist, but no factory holds work.",
    )


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def age_text(value: str, *, now: datetime) -> str:
    updated = parse_iso(value)
    if not updated:
        return "never"
    minutes = max(0, int((now - updated).total_seconds())) // 60
    if minutes < 2:
        return "now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 2880:
        return f"{minutes // 60}h ago"
    return f"{minutes // 1440}d ago"


def collect_opencode_free_roster() -> dict[str, Any]:
    """Load the live OpenCode free-roster probe, never aborting the dashboard."""
    try:
        probe = load_module(
            "probe_opencode_free_roster_dashboard",
            SCRIPTS / "probe_opencode_free_roster.py",
        )
        return probe.collect_roster().as_dict()
    except Exception as exc:
        return {
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "freshness": "stale",
            "detail": (
                "OpenCode free-roster probe failed; table is unknown/stale. "
                f"{exc}"
            ),
            "connections": [],
            "models": [],
            "counts": {},
        }


def collect_snapshot() -> dict[str, Any]:
    completion = load_module(
        "factory_completion_controller_dashboard",
        SCRIPTS / "factory_completion_controller.py",
    )
    full = load_module(
        "factory_full_completion_controller_dashboard",
        SCRIPTS / "factory_full_completion_controller.py",
    )
    work = completion.load_controller()
    issues = work.list_issues()
    prs = work.list_prs()
    demand, capacity = full.current_demand(completion)
    manifest_rows = load_manifest_rows(MANIFEST)
    workers = [row["worker"] for row in manifest_rows]
    owned = completion.owned_worker_ids([*issues, *prs])
    slot_health = capacity.get("slot_health_counts") or {}
    candidate_health = capacity.get("candidate_health_counts") or {}

    hour = iso_search_time(timedelta(hours=1))
    day = iso_search_time(timedelta(hours=24))
    opened_hour = github_search_total(completion, f"is:pr created:>={hour}")
    merged_hour = github_search_total(completion, f"is:pr is:merged merged:>={hour}")
    opened_day = github_search_total(completion, f"is:pr created:>={day}")
    merged_day = github_search_total(completion, f"is:pr is:merged merged:>={day}")

    comments = completion.registry_comments()
    policy = completion.load_policy()
    trusted = [comment for comment in comments if policy.comment_is_trusted(comment)]
    attempts, heartbeats = latest_worker_records(trusted)
    credits = merge_credit(recently_merged_prs(completion))
    worker_rows = build_worker_rows(
        manifest_rows,
        capacity_rows=capacity.get("candidates") or [],
        assignments=current_assignments(issues, prs),
        attempts=attempts,
        heartbeats=heartbeats,
        credits=credits,
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "open_prs": github_search_total(completion, "is:pr is:open"),
        "open_issues": github_search_total(completion, "is:issue is:open"),
        "completion_demand": demand.completion,
        "production_demand": demand.production,
        "completion_share": demand.completion_share,
        "completion_target": full.completion_worker_target(demand),
        "configured_workers": len(workers),
        "busy_workers": len(set(workers) & owned),
        "idle_workers": demand.idle_workers,
        "executable_capacity": capacity.get("executable_capacity", 0),
        "executable_slot_capacity": capacity.get("executable_slot_capacity", 0),
        "healthy_slots": slot_health.get("healthy", 0),
        "degraded_slots": slot_health.get("degraded", 0),
        "cooling_slots": slot_health.get("cooling", 0),
        "unavailable_slots": slot_health.get("unavailable", 0),
        "executable_candidate_count": capacity.get("executable_candidate_count", 0),
        "healthy_candidates": candidate_health.get("healthy", 0),
        "degraded_candidates": candidate_health.get("degraded", 0),
        "cooling_candidates": candidate_health.get("cooling", 0),
        "unavailable_candidates": candidate_health.get("unavailable", 0),
        "pipeline": {
            "review": count_with_label(prs, "factory:review"),
            "changes_requested": count_with_label(prs, "factory:changes-requested"),
            "ci": count_with_label(prs, "factory:ci"),
            "ready": count_with_label(prs, "factory:ready"),
            "unowned": count_with_label(prs, "factory:unowned"),
            "conflict": sum(
                bool({"factory:conflict", "factory:conflicted"} & label_names(pr))
                for pr in prs
            ),
        },
        "throughput": {
            "opened_hour": opened_hour,
            "merged_hour": merged_hour,
            "net_hour": opened_hour - merged_hour,
            "opened_day": opened_day,
            "merged_day": merged_day,
            "net_day": opened_day - merged_day,
        },
        "funnel": latest_completion_funnel(trusted),
        "workers": worker_rows,
        "productive_workers_week": sum(int(row["merged_week"]) > 0 for row in worker_rows),
        "merged_week": sum(int(row["merged_week"]) for row in worker_rows),
        "opencode_free_roster": collect_opencode_free_roster(),
    }


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def signed(value: int) -> str:
    return f"{value:+d}"


def render_work(work: Iterable[Mapping[str, Any]]) -> str:
    items = list(work)
    if not items:
        return '<span class="muted">Idle</span>'
    links = [
        f'<a class="work" href="{esc(item.get("url") or "")}">'
        f'{"PR" if item.get("kind") == "pr" else "Issue"} #{esc(item.get("number") or "")}'
        f'{" · " + esc(item.get("stage") or "") if item.get("stage") else ""}</a>'
        for item in items[:2]
    ]
    if len(items) > 2:
        links.append(f'<span class="muted">+{len(items) - 2} more</span>')
    return "<br>".join(links)


def render_worker_rows(snapshot: Mapping[str, Any]) -> str:
    now = parse_iso(str(snapshot["generated_at"])) or datetime.now(timezone.utc)
    rendered: list[str] = []
    for row in snapshot.get("workers") or []:
        if not isinstance(row, Mapping):
            continue
        tier = str(row.get("tier") or "unproven")
        health = str(row.get("health") or "unknown")
        activity = str(row.get("activity") or "idle")
        attempt = str(row.get("attempt_outcome") or "")
        run = str(row.get("attempt_run") or "")
        attempt_label = (attempt or "no attempt").replace("_", " ")
        attempt_html = (
            f'<a href="{REPO_URL}/actions/runs/{esc(run)}">{esc(attempt_label)} ↗</a>'
            if run
            else esc(attempt_label)
        )
        detail = str(row.get("attempt_detail") or "")
        search = " ".join(
            [
                str(row.get("worker") or ""),
                str(row.get("display_name") or ""),
                str(row.get("runtime_source") or ""),
                str(row.get("runtime_model") or ""),
                attempt,
            ]
        )
        rendered.append(
            f'<tr data-tier="{esc(tier)}" data-health="{esc(health)}" '
            f'data-activity="{esc(activity)}" data-search="{esc(search)}">'
            f'<td><span class="tag {esc(tier)}">{esc(tier)}</span></td>'
            f'<td><strong>#{esc(row.get("worker") or "")}</strong>'
            f'<small>{esc(row.get("display_name") or "")}</small></td>'
            f'<td><span class="tag {esc(health)}">{esc(health)}</span>'
            f'<small>{esc(activity)}</small></td>'
            f'<td><strong>{esc(row.get("runtime_source") or "")}</strong>'
            f'<small>{esc(row.get("runtime_model") or "")}</small></td>'
            f'<td>{attempt_html}{"<small>" + esc(detail) + "</small>" if detail else ""}</td>'
            f'<td class="num">{esc(row.get("merged_day") or 0)}</td>'
            f'<td class="num">{esc(row.get("merged_week") or 0)}</td>'
            f'<td>{render_work(row.get("work") or [])}</td>'
            f'<td>{esc(age_text(str(row.get("updated") or ""), now=now))}</td></tr>'
        )
    return "".join(rendered)


def render_roster_rows(snapshot: Mapping[str, Any]) -> str:
    """Render OpenCode free-roster table rows.

    Args:
        snapshot: Dashboard snapshot that may include ``opencode_free_roster``.

    Returns:
        HTML table rows, or a single unknown/stale placeholder row.
    """
    roster = snapshot.get("opencode_free_roster") or {}
    models = roster.get("models") if isinstance(roster, Mapping) else None
    if not isinstance(models, list) or not models:
        return (
            '<tr><td colspan="4"><span class="muted">'
            "OpenCode free-roster status is unknown/stale."
            "</span></td></tr>"
        )
    rendered: list[str] = []
    for row in models:
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("status") or "unknown")
        detail = str(row.get("detail") or "")
        rendered.append(
            f'<tr data-status="{esc(status)}">'
            f"<td><strong>{esc(row.get('model_id') or '')}</strong>"
            f"<small>{esc(row.get('connection') or '')}</small></td>"
            f'<td><span class="tag {esc(status)}">{esc(status.replace("_", " "))}</span></td>'
            f"<td>{esc(detail)}</td>"
            f"<td>{esc(row.get('http_status') or '—')}</td></tr>"
        )
    return "".join(rendered) or (
        '<tr><td colspan="4"><span class="muted">'
        "OpenCode free-roster status is unknown/stale."
        "</span></td></tr>"
    )


def render_roster_section(snapshot: Mapping[str, Any]) -> str:
    """Render the OpenCode free-roster panel.

    Args:
        snapshot: Dashboard snapshot that may include ``opencode_free_roster``.

    Returns:
        HTML for the roster section, including last-checked time.
    """
    roster = snapshot.get("opencode_free_roster")
    if not isinstance(roster, Mapping):
        roster = {}
    checked = str(roster.get("checked_at") or snapshot.get("generated_at") or "unknown")
    freshness = str(roster.get("freshness") or "stale")
    detail = str(
        roster.get("detail")
        or (
            "Live OmniRoute probe of free OpenCode / Zen models. "
            "opencode and opencode-zen connections can both be active. "
            "Free roster models stay eligible via catalog, pricing, and freeProviders; "
            "the opencode-zen providerOverride (blanket whole-provider=free) is avoided "
            "to prevent paid bleed."
        )
    )
    counts = roster.get("counts") if isinstance(roster.get("counts"), Mapping) else {}
    count_text = " · ".join(
        f"{int(counts.get(key, 0))} {key.replace('_', ' ')}"
        for key in (
            "ok",
            "rate_limited",
            "unsupported",
            "catalog_miss",
            "auth_paid",
            "timeout",
            "error",
            "unknown",
        )
        if int(counts.get(key, 0))
    ) or "no live rows"
    connections = roster.get("connections") if isinstance(roster.get("connections"), list) else []
    connection_html = "".join(
        f'<span class="tag {("healthy" if item.get("active") else "unknown")}">'
        f"{esc(item.get('provider') or '')} · {esc(item.get('detail') or 'unknown')}</span>"
        for item in connections
        if isinstance(item, Mapping)
    )
    if not connection_html:
        connection_html = (
            '<span class="tag unknown">opencode · unknown</span>'
            '<span class="tag unknown">opencode-zen · unknown</span>'
        )
    return f"""<section class="panel scoreboard" id="opencode-free-roster">
<div class="score-head"><div><h2>OpenCode free roster</h2>
<div class="sub">{esc(detail)}</div></div>
<div class="sub">Last checked {esc(checked)} · {esc(freshness)} · {esc(count_text)}</div></div>
<div class="notes">{connection_html}</div>
<div class="table-wrap"><table><thead><tr><th>Model</th><th>Status</th><th>Detail</th><th>HTTP</th></tr></thead>
<tbody id="opencode-free-roster-rows">{render_roster_rows(snapshot)}</tbody></table></div>
<div class="notes">Free OpenCode models remain first-class factory capacity. This table does not disable Zen; both connections can stay active. Statuses are live-ish OmniRoute probes, not a historical success rate.</div>
</section>"""


def render_dashboard(snapshot: Mapping[str, Any]) -> str:
    pipeline = snapshot["pipeline"]
    throughput = snapshot["throughput"]
    funnel = snapshot.get("funnel") or {}
    completion_pct = round(float(snapshot["completion_share"]) * 100)
    production_pct = (
        100 - completion_pct
        if snapshot["completion_demand"] or snapshot["production_demand"]
        else 0
    )
    verdict_class, verdict_title, verdict_detail = system_verdict(snapshot)
    cards = [
        ("Open PRs", snapshot["open_prs"], f"{REPO_URL}/pulls"),
        ("Working now", snapshot["busy_workers"], f"{REPO_URL}/issues/1093"),
        ("Executable slots", snapshot["executable_slot_capacity"], f"{REPO_URL}/issues/1093"),
        ("7d merge credit", snapshot.get("merged_week", 0), f"{REPO_URL}/pulls?q=is%3Amerged"),
    ]
    cards_html = "".join(
        f'<a class="metric" href="{url}"><span>{label}</span><strong>{esc(value)}</strong></a>'
        for label, value, url in cards
    )
    pipeline_html = "".join(
        f'<a class="row" href="{REPO_URL}/pulls?q=is%3Aopen+label%3A{query}">'
        f"<span>{label}</span><strong>{esc(pipeline[key])}</strong></a>"
        for label, key, query in [
            ("Review", "review", "factory%3Areview"),
            ("Changes requested", "changes_requested", "factory%3Achanges-requested"),
            ("CI", "ci", "factory%3Aci"),
            ("Ready", "ready", "factory%3Aready"),
            ("Unowned", "unowned", "factory%3Aunowned"),
            ("Conflict", "conflict", "factory%3Aconflict"),
        ]
    )
    funnel_text = "Latest allocator telemetry unavailable"
    if funnel:
        funnel_text = (
            f"target {funnel.get('completion target', '?')} · "
            f"selected {funnel.get('workers selected', '?')} · "
            f"claims {funnel.get('pr claims succeeded', '?')} · "
            f"{funnel.get('updated', 'unknown')}"
        )

    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300"><title>ComicPile Factory Status</title>
<style>
:root{{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#f0f6fc;--muted:#8b949e;--good:#3fb950;--warn:#d29922;--bad:#f85149;--link:#58a6ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}}main{{max-width:1380px;margin:auto;padding:28px 20px 60px}}a{{color:inherit;text-decoration:none}}a:hover,.work{{color:var(--link)}}header,.score-head,.filters{{display:flex;gap:12px;justify-content:space-between;align-items:flex-end;flex-wrap:wrap}}header{{margin-bottom:18px}}h1{{margin:0;font-size:28px}}h2{{font-size:15px;margin:0 0 12px;color:var(--muted)}}.sub,small{{display:block;color:var(--muted);font-size:12px}}.verdict,.metric,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px}}.verdict{{display:flex;gap:12px;align-items:center;padding:15px 17px;margin-bottom:12px}}.dot{{width:14px;height:14px;border-radius:50%;background:var(--muted)}}.verdict.good .dot{{background:var(--good)}}.verdict.warn .dot{{background:var(--warn)}}.verdict.bad .dot{{background:var(--bad)}}.verdict strong{{font-size:20px;margin-right:8px}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.metric{{padding:15px;min-height:90px;display:flex;flex-direction:column;justify-content:space-between}}.metric span,.muted{{color:var(--muted)}}.metric strong,.big{{font-size:28px}}.grid{{display:grid;grid-template-columns:1.15fr 1fr 1fr;gap:12px;margin-top:12px}}.panel{{padding:15px;min-width:0}}.row{{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid var(--line)}}.row:first-of-type{{border-top:0}}.good{{color:var(--good)}}.bad{{color:var(--bad)}}.ratio{{height:16px;display:flex;overflow:hidden;border-radius:99px;background:#21262d;margin:11px 0}}.completion{{width:{completion_pct}%;background:var(--link)}}.production{{width:{production_pct}%;background:var(--good)}}.scoreboard{{margin-top:12px}}.score-head h2{{font-size:18px;color:var(--text);margin-bottom:2px}}.filters input,.filters select{{min-height:38px;padding:8px 10px;background:#0f141b;color:var(--text);border:1px solid var(--line);border-radius:8px}}.filters input{{width:min(280px,70vw)}}.table-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:9px}}table{{width:100%;min-width:1040px;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}}th{{font-size:11px;text-transform:uppercase;color:var(--muted);background:#1b222c}}.num{{text-align:center;font-weight:700}}.tag{{display:inline-block;padding:2px 7px;border:1px solid var(--line);border-radius:99px;font-size:10px;text-transform:uppercase;font-weight:700}}.proven,.productive,.healthy,.ok{{color:var(--good);border-color:#238636}}.working{{color:var(--link);border-color:#1f6feb}}.watch,.degraded,.cooling,.rate_limited,.timeout,.catalog_miss,.unsupported{{color:var(--warn);border-color:#9e6a03}}.blocked,.unavailable,.auth_paid,.error{{color:var(--bad);border-color:#da3633}}.unknown,.stale{{color:var(--muted)}}.notes,footer{{color:var(--muted);font-size:12px;margin-top:10px}}#opencode-free-roster .notes .tag{{margin:0 8px 8px 0}}.scoreboard+.scoreboard{{margin-top:16px}}@media(max-width:900px){{.metrics{{grid-template-columns:repeat(2,1fr)}}.grid{{grid-template-columns:1fr}}main{{padding:18px 12px 40px}}}}
</style></head><body><main>
<header><div><h1>🏭 ComicPile Factory</h1><div class="sub">Can the fleet move work, and which factories are actually useful?</div></div><div class="sub">Generated {esc(snapshot['generated_at'])} · refreshes every 5 min · <a href="{REPO_URL}/actions">Actions ↗</a></div></header>
<section class="verdict {esc(verdict_class)}"><span class="dot"></span><div><strong>{esc(verdict_title)}</strong>{esc(verdict_detail)}</div></section>
<section class="metrics">{cards_html}</section>
<section class="grid">
<div class="panel"><h2>Live allocation</h2><div><span class="big">{completion_pct}%</span> completion · <span class="big">{production_pct}%</span> production</div><div class="ratio"><div class="completion"></div><div class="production"></div></div><div class="muted">Target completion workers: <strong>{esc(snapshot['completion_target'])}</strong> from {esc(snapshot['idle_workers'])} currently idle.</div><div class="sub">{esc(funnel_text)}</div></div>
<div class="panel"><h2>Fleet capacity</h2><div class="row"><span>Executable slots</span><strong class="good">{esc(snapshot['executable_slot_capacity'])}</strong></div><div class="row"><span>Executable provider/models</span><strong>{esc(snapshot['executable_candidate_count'])}</strong></div><div class="row"><span>Slot health</span><strong>{esc(snapshot['healthy_slots'])} healthy · {esc(snapshot['degraded_slots'])} degraded</strong></div><div class="row"><span>Excluded</span><strong>{esc(snapshot['cooling_slots'])} cooling · {esc(snapshot['unavailable_slots'])} unavailable</strong></div><div class="sub">{esc(snapshot['busy_workers'])} working · {esc(snapshot['idle_workers'])} idle executable · {esc(snapshot['configured_workers'])} configured</div></div>
<div class="panel"><h2>PR pipeline</h2>{pipeline_html}</div>
<div class="panel"><h2>Throughput · last hour</h2><div class="row"><span>Opened</span><strong>{esc(throughput['opened_hour'])}</strong></div><div class="row"><span>Merged</span><strong>{esc(throughput['merged_hour'])}</strong></div><div class="row"><span>Net PR change</span><strong class="{'good' if throughput['net_hour'] < 0 else 'bad' if throughput['net_hour'] > 0 else ''}">{signed(int(throughput['net_hour']))}</strong></div></div>
<div class="panel"><h2>Throughput · last 24h</h2><div class="row"><span>Opened</span><strong>{esc(throughput['opened_day'])}</strong></div><div class="row"><span>Merged</span><strong>{esc(throughput['merged_day'])}</strong></div><div class="row"><span>Net PR change</span><strong class="{'good' if throughput['net_day'] < 0 else 'bad' if throughput['net_day'] > 0 else ''}">{signed(int(throughput['net_day']))}</strong></div><div class="row"><span>Factories with 7d merge credit</span><strong>{esc(snapshot.get('productive_workers_week', 0))}</strong></div></div>
<div class="panel"><h2>Control plane</h2><a class="row" href="{REPO_URL}/issues/1093"><span>Heartbeat + allocator registry</span><strong>#1093 ↗</strong></a><a class="row" href="{REPO_URL}/actions/workflows/factory-completion-drain.yml"><span>Completion drain</span><strong>workflow ↗</strong></a><a class="row" href="{REPO_URL}/actions/workflows/fixed-model-factory-dispatch.yml"><span>Main dispatcher</span><strong>workflow ↗</strong></a></div>
</section>
{render_roster_section(snapshot)}
<section class="panel scoreboard"><div class="score-head"><div><h2>Fleet scoreboard</h2><div class="sub">Current health, current work, latest classified attempt, and retained merge credit.</div></div><div class="filters"><input id="factory-search" type="search" placeholder="Factory, model, provider…" aria-label="Search factories"><select id="factory-filter" aria-label="Filter factories"><option value="">All factories</option><option value="working">Working now</option><option value="healthy">Healthy</option><option value="watch">Needs attention</option><option value="blocked">Blocked</option></select></div></div>
<div class="table-wrap"><table><thead><tr><th>Verdict</th><th>Factory</th><th>Health</th><th>Latest runtime</th><th>Latest attempt</th><th>24h merges</th><th>7d merges</th><th>Current work</th><th>Seen</th></tr></thead><tbody id="factory-rows">{render_worker_rows(snapshot)}</tbody></table></div>
<div class="notes">7d merge credit uses the factory owner label retained on merged PRs. The registry currently overwrites each factory’s latest attempt, so this dashboard does not pretend that one latest outcome is a historical success rate.</div></section>
<footer>Read-only projection of GitHub factory state. GitHub labels, leases, workflow state, and allocator policy remain authoritative.</footer>
<script>(()=>{{const q=document.getElementById('factory-search'),f=document.getElementById('factory-filter'),rows=[...document.querySelectorAll('#factory-rows tr')];const apply=()=>{{const s=q.value.trim().toLowerCase(),v=f.value;rows.forEach(r=>{{let ok=!s||(r.dataset.search||'').toLowerCase().includes(s);if(v==='working')ok=ok&&r.dataset.activity==='working';if(v==='healthy')ok=ok&&r.dataset.health==='healthy';if(v==='watch')ok=ok&&r.dataset.tier==='watch';if(v==='blocked')ok=ok&&r.dataset.tier==='blocked';r.hidden=!ok}})}};q.addEventListener('input',apply);f.addEventListener('change',apply)}})();</script>
</main></body></html>"""


def main() -> int:
    snapshot = collect_snapshot()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_dashboard(snapshot), encoding="utf-8")
    print(json.dumps(snapshot, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
