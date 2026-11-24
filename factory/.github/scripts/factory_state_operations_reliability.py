#!/usr/bin/env python3
"""Per-model reliability snapshot from trusted terminal worker events."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import factory_state_operations_core as core


RELIABILITY_WINDOWS = {"24h": "24 hours", "7d": "7 days"}


def summarize_reliability(rows: list[dict[str, Any]], health: list[dict[str, Any]],
                          generated_at: datetime) -> dict[str, Any]:
    """Aggregate trusted terminal worker events into per-model reliability.

    ``rows`` carry window, source, model, event_type, outcome_class and
    delivered. Counts are observations only: no score is invented, and a
    model with few runs is reported as such rather than ranked.
    """
    models: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["source"] or "unknown"), str(row["model"] or "unclassified"))
        entry = models.setdefault(key, {
            "source": key[0], "model": key[1],
            "windows": {name: {"runs": 0, "delivered": 0, "completed": 0, "idle": 0,
                               "failed": 0, "failures": {}} for name in RELIABILITY_WINDOWS},
        })
        window = entry["windows"][row["window"]]
        count = int(row.get("n") or 1)
        window["runs"] += count
        if row["event_type"] == "worker_completed":
            window["completed"] += count
            if row.get("delivered"):
                window["delivered"] += count
        elif row["event_type"] == "worker_idle":
            window["idle"] += count
        else:
            window["failed"] += count
            reason = str(row.get("outcome_class") or "unknown_failure")
            window["failures"][reason] = window["failures"].get(reason, 0) + count
    for row in health:
        key = (str(row["source"]), str(row["model"]))
        entry = models.setdefault(key, {
            "source": key[0], "model": key[1],
            "windows": {name: {"runs": 0, "delivered": 0, "completed": 0, "idle": 0,
                               "failed": 0, "failures": {}} for name in RELIABILITY_WINDOWS},
        })
        until = row.get("cooldown_until")
        entry["health"] = {
            "status": row.get("status"), "last_outcome": row.get("last_outcome"),
            "cooldown_until": until.isoformat() if until is not None else None,
        }
    providers: dict[str, dict[str, Any]] = {}
    for entry in models.values():
        provider = providers.setdefault(entry["source"], {
            "source": entry["source"],
            "windows": {name: {"runs": 0, "delivered": 0, "completed": 0, "idle": 0, "failed": 0}
                        for name in RELIABILITY_WINDOWS},
        })
        for name, window in entry["windows"].items():
            for field in ("runs", "delivered", "completed", "idle", "failed"):
                provider["windows"][name][field] += window[field]
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "windows": RELIABILITY_WINDOWS,
        "providers": sorted(providers.values(), key=lambda item: item["source"]),
        "models": sorted(models.values(), key=lambda item: (item["source"], item["model"])),
    }


async def _export_reliability(conn: Any, repo: str) -> dict[str, Any]:
    rows = []
    for window, interval in RELIABILITY_WINDOWS.items():
        for row in await conn.fetch(
            f"""SELECT detail->>'source' AS source, detail->>'model' AS model, event_type,
                       detail->>'outcome_class' AS outcome_class,
                       (detail->>'delivery_outcome') = 'persisted' AS delivered,
                       count(*) AS n
                  FROM public.factory_state_events
                 WHERE subject_type='worker'
                   AND event_type IN ('worker_completed','worker_idle','worker_failed')
                   AND created_at > now() - interval '{interval}'
                 GROUP BY 1,2,3,4,5"""
        ):
            rows.append(dict(row) | {"window": window})
    health = [dict(row) for row in await conn.fetch(
        """SELECT source, model, status, last_outcome, cooldown_until
             FROM public.factory_model_health WHERE repo=$1""", repo)]
    return summarize_reliability(rows, health, datetime.now(timezone.utc))


def export_reliability(repo: str) -> dict[str, Any]:
    """Publishable reliability snapshot; contains counts only, never logs or secrets."""
    if not core.enabled():
        raise RuntimeError("Factory reliability requires the trusted Neon connection")
    return core._run(_export_reliability, repo)
