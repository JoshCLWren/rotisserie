"""Turso monthly usage against the free plan, for the CLI, doctor and both UIs.

The trusted status page job reads the Turso Platform API and publishes the
result as ``db_usage`` inside ``factory-stats.json``. Doctor and the default
CLI read that published value, so they make no extra API call. ``--via github``
asks the label claim workflow for a fresh read; ``--live`` reads the API
directly and works only where the token is in the environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, obj, timestamp
from rotisserie.factory_console.service import bridge, reliability
from rotisserie.factory_console.tracker import Clock

LABEL_WORKFLOW = "rotisserie-label-claim.yml"
LABEL_ARTIFACT = "rotisserie-label-claim-{run_id}"
STALE = timedelta(hours=1)
LABELS = {
    "rows_read": "rows read",
    "rows_written": "rows written",
    "storage_bytes": "storage",
    "bytes_synced": "sync",
}
DOCTOR_STATUS = {"ok": "ok", "warn": "warn", "critical": "fail", "not_configured": "ok"}


def _module() -> Any:
    return runtime.module("factory_turso_usage")


def from_stats(stats: Obj | None) -> dict[str, Any]:
    report = obj((stats or {}).get("db_usage"))
    if not report:
        return {
            "status": "unpublished",
            "reasons": ["no Turso usage has been published yet"],
            "source": "published",
        }
    return {**report, "source": "published"}


def published(github: GitHub) -> dict[str, Any]:
    return from_stats(reliability.load_stats(github))


def live(env: Mapping[str, str]) -> dict[str, Any]:
    return {**_module().from_env(env), "source": "turso api"}


def via_github(
    github: GitHub, clock: Clock, *, started: dict[str, object] | None = None
) -> dict[str, Any]:
    result = bridge.run(
        github,
        clock,
        LABEL_WORKFLOW,
        {"mode": "db-usage"},
        artifact=LABEL_ARTIFACT,
        timeout=bridge.CLAIM_TIMEOUT,
        started=started,
    )
    return {**result.data, "source": "label claim workflow", "bridge": result.summary()}


def pause_new_claims(report: Mapping[str, Any] | None) -> tuple[bool, str]:
    decision: tuple[bool, str] = _module().pause_new_claims(report)
    return decision


def amount(metric: str, value: object) -> str:
    number = float(value) if isinstance(value, int | float) else 0.0
    if metric in ("storage_bytes", "bytes_synced"):
        for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
            if number >= size:
                return f"{number / size:.1f} {unit}"
        return f"{int(number)} B"
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if number >= size:
            return f"{number / size:.1f}{unit}"
    return str(int(number))


def metric_lines(report: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    """(name, used of allowance, share, projected) for each metric."""
    rows: list[tuple[str, str, str, str]] = []
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        return rows
    for metric, label in LABELS.items():
        entry = metrics.get(metric)
        if not isinstance(entry, dict):
            continue
        share = entry.get("share_used")
        rows.append(
            (
                label,
                f"{amount(metric, entry.get('organization'))} of "
                f"{amount(metric, entry.get('allowance'))}",
                f"{share:.1%}" if isinstance(share, int | float) else "?",
                amount(metric, entry.get("projected_organization")),
            )
        )
    return rows


def summary(report: Mapping[str, Any]) -> str:
    parts = [f"{label} {used} ({share})" for label, used, share, _ in metric_lines(report)[:3]]
    if report.get("resets_at"):
        parts.append(f"resets {str(report['resets_at'])[:10]} (hour unverified)")
    reasons = [str(r) for r in report.get("reasons") or []]
    text = "; ".join(parts) or "; ".join(reasons) or str(report.get("status"))
    if parts and reasons:
        text += " | " + "; ".join(reasons)
    label = report.get("label")
    return f"{label}: {text}" if label else text


def health(stats: Obj | None, now: datetime) -> tuple[str, str, str]:
    """(status, text, hint) for the doctor line, from the published value only."""
    report = from_stats(stats)
    state = str(report.get("status"))
    if state == "unpublished":
        return "ok", "no Turso usage published yet (not in use yet)", ""
    checked = timestamp(report, "checked_at")
    status = DOCTOR_STATUS.get(state, "warn")
    hint = "rotisserie factory db-usage --via github reads it fresh"
    if state != "not_configured" and (checked is None or now - checked > STALE):
        if status == "ok":
            status = "warn"
        return status, "published Turso usage is stale: " + summary(report), hint
    if status == "fail":
        hint = "new claims pause at critical; " + hint
    return status, summary(report), "" if status == "ok" else hint
