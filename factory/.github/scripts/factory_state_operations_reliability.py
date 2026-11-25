#!/usr/bin/env python3
"""Per-model reliability snapshot from trusted terminal worker events."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


RELIABILITY_WINDOWS = {"24h": "24 hours", "7d": "7 days"}


def _tally(window: dict[str, Any], row: dict[str, Any], count: int) -> None:
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
        _tally(window, row, count)
        kind = row.get("action_kind")
        if isinstance(kind, str) and kind:
            kinds = window.setdefault("kinds", {})
            _tally(kinds.setdefault(kind, {"runs": 0, "delivered": 0, "completed": 0, "idle": 0,
                                            "failed": 0, "failures": {}}), row, count)
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
    found = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "windows": RELIABILITY_WINDOWS,
        "providers": sorted(providers.values(), key=lambda item: item["source"]),
        "models": sorted(models.values(), key=lambda item: (item["source"], item["model"])),
    }
    from factory_track_record_cooldown import hot_models

    for key, heat in hot_models(found, generated_at).items():
        models[key]["kept_hot"] = {"successes": heat.successes, "trials": heat.trials}
    return found
