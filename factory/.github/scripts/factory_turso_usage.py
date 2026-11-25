#!/usr/bin/env python3
"""Turso monthly usage, the free-plan allowance, and whether to pause new claims.

Standard library only: the status page job runs this file without installing
anything, the Rotisserie CLI loads it as a factory runtime module, and the
Turso persistence code imports ``pause_new_claims``. The Platform API token
comes from ``FACTORY_TURSO_API_TOKEN`` and never appears in output or errors.

Turso counts usage per calendar month. The exact reset hour is unverified, so
the reset time shown is the start of the next UTC calendar month.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

API = "https://api.turso.tech/v1"
METRICS = ("rows_read", "rows_written", "storage_bytes", "bytes_synced")
CUMULATIVE = ("rows_read", "rows_written", "bytes_synced")
# Documented free plan (docs.turso.tech, Nov 24 2026); used when /plans has no quotas.
FREE_PLAN = {
    "rows_read": 500_000_000,
    "rows_written": 10_000_000,
    "storage_bytes": 5_000_000_000,
    "bytes_synced": 3_000_000_000,
}
PLAN_QUOTA_KEYS = {
    "rows_read": "rowsRead",
    "rows_written": "rowsWritten",
    "storage_bytes": "storage",
    "bytes_synced": "bytesSynced",
}
# Owner engineering targets for the factory database (#253).
OWNER_TARGETS = {"rows_read": 50_000_000, "rows_written": 1_000_000, "storage_bytes": 250_000_000}
WARN_SHARE = 0.70
CRITICAL_SHARE = 0.90
# A straight line from the first hours of a month is noise; project from at least a day.
MIN_ELAPSED_DAYS = 1.0
BLOCKED = re.compile(r"\bblocked\b", re.IGNORECASE)

Fetch = Callable[[str, str], Any]


class UsageError(RuntimeError):
    """The usage API could not be read; the message never holds the token."""


def parse_database_url(url: str, org: str = "", database: str = "") -> tuple[str, str]:
    """``https://{database}-{org}.{region}.turso.io`` to (org, database).

    Both names may contain hyphens, so the split at the last hyphen is only a
    default; ``org`` or ``database`` from adopter configuration wins.
    """
    host = urllib.parse.urlsplit(url if "://" in url else f"https://{url}").hostname or ""
    label = host.split(".", 1)[0]
    if org and database:
        return org, database
    if org and label.endswith(f"-{org}"):
        return org, label[: -len(org) - 1]
    if database and label.startswith(f"{database}-"):
        return label[len(database) + 1 :], database
    name, _, slug = label.rpartition("-")
    if not name or not slug or not host.endswith(".turso.io"):
        raise UsageError("FACTORY_TURSO_URL is not a Turso database URL")
    return org or slug, database or name


def month_window(now: datetime) -> dict[str, Any]:
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    reset = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    days = (reset - start).total_seconds() / 86400
    elapsed = (now - start).total_seconds() / 86400
    return {
        "month_start": start.isoformat(),
        "resets_at": reset.isoformat(),
        "reset_hour_verified": False,
        "days_in_month": round(days, 3),
        "days_elapsed": round(elapsed, 3),
        "days_left": round(max(0.0, days - elapsed), 2),
    }


def project(used: Mapping[str, int], window: Mapping[str, Any]) -> dict[str, int]:
    """Straight line to month end for monthly counters; storage stays as it stands."""
    elapsed = max(float(window["days_elapsed"]), MIN_ELAPSED_DAYS)
    days = float(window["days_in_month"])
    projected: dict[str, int] = {}
    for metric in METRICS:
        value = int(used.get(metric) or 0)
        projected[metric] = round(value * days / elapsed) if metric in CUMULATIVE else value
    return projected


def _share(value: int, allowance: int) -> float | None:
    return round(value / allowance, 4) if allowance > 0 else None


def evaluate(
    *,
    database: Mapping[str, int],
    organization: Mapping[str, int],
    allowances: Mapping[str, int],
    allowance_source: str,
    blocked: Mapping[str, bool],
    overages: bool,
    now: datetime,
    in_use: bool,
) -> dict[str, Any]:
    """The published report. Critical uses actual shares; warn uses projections."""
    window = month_window(now)
    projected_org = project(organization, window)
    projected_db = project(database, window)
    metrics: dict[str, Any] = {}
    reasons: list[str] = []
    critical: list[str] = []
    for metric in METRICS:
        allowance = int(allowances.get(metric) or 0)
        share = _share(int(organization.get(metric) or 0), allowance)
        projected_share = _share(projected_org[metric], allowance)
        target = OWNER_TARGETS.get(metric)
        metrics[metric] = {
            "database": int(database.get(metric) or 0),
            "organization": int(organization.get(metric) or 0),
            "allowance": allowance,
            "share_used": share,
            "projected_database": projected_db[metric],
            "projected_organization": projected_org[metric],
            "projected_share": projected_share,
            "owner_target": target,
        }
        if share is not None and share > CRITICAL_SHARE:
            critical.append(f"{metric} at {share:.0%} of the allowance")
        if projected_share is not None and projected_share > WARN_SHARE:
            reasons.append(f"{metric} projected at {projected_share:.0%} of the allowance")
        if target is not None and projected_db[metric] > target:
            reasons.append(f"{metric} projected over the owner target {target:,}")
    if blocked.get("reads") or blocked.get("writes"):
        critical.insert(0, "Turso reports BLOCKED " + "/".join(k for k, v in blocked.items() if v))
    if overages:
        reasons.append("overages are enabled; the factory budget is $0")
    status = "critical" if critical else "warn" if reasons else "ok"
    return {
        "status": status,
        "reasons": critical + reasons,
        "in_use": in_use,
        "label": "" if in_use else "not in use yet",
        "checked_at": now.astimezone(UTC).isoformat(),
        "blocked": {"reads": bool(blocked.get("reads")), "writes": bool(blocked.get("writes"))},
        "overages": overages,
        "allowance_source": allowance_source,
        **window,
        "metrics": metrics,
    }


def not_configured(now: datetime, in_use: bool, missing: list[str]) -> dict[str, Any]:
    return {
        "status": "not_configured",
        "reasons": [f"not configured: {', '.join(missing)} missing"],
        "in_use": in_use,
        "label": "" if in_use else "not in use yet",
        "checked_at": now.astimezone(UTC).isoformat(),
    }


def failed(now: datetime, in_use: bool, error: str) -> dict[str, Any]:
    return {
        "status": "error",
        "reasons": [error],
        "in_use": in_use,
        "label": "" if in_use else "not in use yet",
        "checked_at": now.astimezone(UTC).isoformat(),
    }


def http_fetch(token: str) -> Fetch:
    def get(path: str, what: str) -> Any:
        request = urllib.request.Request(
            API + path,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "rotisserie-factory"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            hint = " (check FACTORY_TURSO_ORG / FACTORY_TURSO_DATABASE)" if exc.code == 404 else ""
            raise UsageError(f"Turso {what} answered HTTP {exc.code}{hint}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise UsageError(f"Turso {what} could not be read: {type(exc).__name__}") from None

    return get


def _counts(value: Any) -> dict[str, int]:
    """Usage counters, or an error when the block or a metric is missing.

    A missing block must not read as zero usage: zero is also what an empty
    database reports, so a changed response shape would otherwise look healthy.
    """
    if not isinstance(value, dict) or not all(
        isinstance(value.get(m), int | float) for m in METRICS
    ):
        raise UsageError("Turso usage has an unexpected response shape")
    return {m: int(value[m]) for m in METRICS}


def _block(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return None
    return entry.get("total") if "total" in entry else entry.get("usage")


def _allowances(plans: Any, plan_id: str) -> tuple[dict[str, int], str]:
    for plan in (plans or {}).get("plans") or []:
        quotas = plan.get("quotas") if isinstance(plan, dict) else None
        if plan_id and plan.get("name") == plan_id and isinstance(quotas, dict):
            found = {m: int(quotas.get(k) or 0) for m, k in PLAN_QUOTA_KEYS.items()}
            if all(found.values()):
                return found, f"turso plan {plan_id}"
    return dict(FREE_PLAN), "documented free plan"


def read(fetch: Fetch, org: str, database: str, *, now: datetime, in_use: bool) -> dict[str, Any]:
    base = "/organizations/" + urllib.parse.quote(org, safe="")
    details = fetch(base, "organization").get("organization") or {}
    blocked = {
        "reads": bool(details.get("blocked_reads")),
        "writes": bool(details.get("blocked_writes")),
    }
    try:
        return _read_usage(fetch, base, database, details, blocked, now=now, in_use=in_use)
    except (UsageError, AttributeError, TypeError, KeyError) as exc:
        if not any(blocked.values()):
            raise
        message = str(exc) if isinstance(exc, UsageError) else "unexpected response shape"
        report = failed(now, in_use, message)
        report["reasons"].insert(0, "Turso reports BLOCKED")
        return {**report, "status": "critical", "blocked": blocked}


def _read_usage(
    fetch: Fetch,
    base: str,
    database: str,
    details: Mapping[str, Any],
    blocked: Mapping[str, bool],
    *,
    now: datetime,
    in_use: bool,
) -> dict[str, Any]:
    usage = fetch(base + "/usage", "organization usage")
    own = fetch(f"{base}/databases/{urllib.parse.quote(database, safe='')}/usage", "database usage")
    try:
        plans = fetch(base + "/plans", "plans")
    except UsageError:
        plans = None
    listed = usage.get("databases")
    if listed is None:
        listed = (usage.get("organization") or {}).get("databases")
    mine = _counts(_block((own or {}).get("database")))
    if isinstance(listed, list) and listed:
        totals = [_counts(_block(entry)) for entry in listed]
        organization = {m: sum(t[m] for t in totals) for m in METRICS}
        organization_source = "sum of database totals"
    else:
        organization = dict(mine)
        organization_source = "factory database only (organization list missing)"
    allowances, source = _allowances(plans, str(details.get("plan_id") or ""))
    report = evaluate(
        database=mine,
        organization=organization,
        allowances=allowances,
        allowance_source=source,
        blocked=blocked,
        overages=bool(details.get("overages")),
        now=now,
        in_use=in_use,
    )
    return {**report, "organization_source": organization_source}


def from_env(
    env: Mapping[str, str], *, now: datetime | None = None, fetch: Fetch | None = None
) -> dict[str, Any]:
    """One trusted read. Turso is in use wherever the workflow has the store URL."""
    moment = now or datetime.now(UTC)
    in_use = bool(env.get("FACTORY_TURSO_URL", ""))
    token = env.get("FACTORY_TURSO_API_TOKEN", "")
    url = env.get("FACTORY_TURSO_URL", "")
    missing = [
        n for n, v in (("FACTORY_TURSO_API_TOKEN", token), ("FACTORY_TURSO_URL", url)) if not v
    ]
    if missing:
        return not_configured(moment, in_use, missing)
    try:
        org, database = parse_database_url(
            url, env.get("FACTORY_TURSO_ORG", ""), env.get("FACTORY_TURSO_DATABASE", "")
        )
        return read(fetch or http_fetch(token), org, database, now=moment, in_use=in_use)
    except UsageError as exc:
        return failed(moment, in_use, str(exc))
    except (AttributeError, TypeError, KeyError):
        return failed(moment, in_use, "Turso usage has an unexpected response shape")


def is_blocked(error: BaseException | str) -> bool:
    """A libSQL answer that says the quota blocked the operation."""
    return bool(BLOCKED.search(str(error)))


def pause_new_claims(
    report: Mapping[str, Any] | None = None, error: BaseException | str | None = None
) -> tuple[bool, str]:
    """Pause new claims when usage is critical or the database answers BLOCKED.

    An unknown, stale or missing report never pauses: it is advisory, and the
    database itself still fails closed when it is unavailable.
    """
    if error is not None and is_blocked(error):
        return True, "the Turso database answered BLOCKED; the monthly quota is exhausted"
    if report and report.get("status") == "critical":
        reasons = report.get("reasons") or ["critical"]
        return True, "Turso usage is critical: " + "; ".join(str(r) for r in reasons)
    return False, ""


def main() -> int:
    json.dump(from_env(os.environ), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    raise SystemExit(main())
