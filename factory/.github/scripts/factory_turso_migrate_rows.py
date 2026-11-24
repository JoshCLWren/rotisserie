"""Neon events and typed operations-table rows mapped to libSQL store rows.

See ``factory_turso_migrate_map`` for the conventions: one Neon row in, a
``Mapped`` destination row or a counted skip reason out.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from factory_turso_migrate_map import (
    EVENT_FIELDS,
    HEALTH_STATUSES,
    REPO_RE,
    Invalid,
    Mapped,
    _done,
    _int,
    _json,
    _text,
    epoch,
)


def event_key(row: dict[str, Any]) -> str:
    """The event's own ``event_id``, or a fixed ``neon:<id>`` so a re-run finds it again."""
    detail = _json(row.get("detail"))
    found = detail.get("event_id") if isinstance(detail, dict) else None
    return f"neon:{row['id']}" if found is None or found == "" else str(found)


def map_event(row: dict[str, Any], default_repo: str) -> Mapped:
    """One Neon event as ``event_insert`` writes it: ``source`` becomes ``origin``."""
    group = "factory_state_events"
    try:
        detail = _json(row.get("detail"))
        if not isinstance(detail, dict):
            raise Invalid("detail_invalid")
        repo = detail.get("repo") or detail.get("repository")
        defaulted = not isinstance(repo, str) or not REPO_RE.fullmatch(repo)
        repo = default_repo if defaulted else repo
        values: dict[str, Any] = {
            "repo": repo,
            "origin": _text(row.get("source")),
            "event_type": _text(row.get("event_type")),
            "subject_type": _text(row.get("subject_type")),
            "subject_number": _int(row.get("subject_number"), None),
            "created_at": epoch(row.get("created_at")),
            "detail": json.dumps({**detail, "repo": repo}, sort_keys=True, default=str),
        }
        for field in EVENT_FIELDS:
            value = detail.get(field)
            values[field] = None if value is None or value == "" else str(value)
        values["event_id"] = event_key(row)
        mapped = _done(group, "factory_state_events", values)
    except Invalid as error:
        return Mapped(group, reason=str(error))
    return Mapped(group, mapped.table, mapped.values, "repo_defaulted" if defaulted else "")


def map_table(table: str, row: dict[str, Any], now: int) -> Mapped:
    """Rows of the typed Neon operations tables; ``now`` is Neon's clock."""
    try:
        values = TABLE_MAPPERS[table](row, now)
        if isinstance(values, str):
            return Mapped(table, reason=values)
        return _done(table, table, values)
    except Invalid as error:
        return Mapped(table, reason=str(error))
    except KeyError, TypeError:
        return Mapped(table, reason="row_invalid")


def _health(row: dict[str, Any], now: int) -> dict[str, Any] | str:
    status = str(row["status"])
    if status not in HEALTH_STATUSES:
        return "status_invalid"
    return {
        "repo": row["repo"],
        "source": row["source"],
        "model": row["model"],
        "status": status,
        "last_outcome": _text(row.get("last_outcome")),
        "cooldown_until": epoch(row.get("cooldown_until")),
        "last_verified_at": epoch(row.get("last_verified_at")),
        "last_run_id": _int(row.get("last_run_id")),
        "updated_at": epoch(row.get("updated_at")),
    }


def _metrics(row: dict[str, Any], now: int) -> dict[str, Any]:
    counters = ("attempts", "productive", "completed", "failures", "last_run_id")
    return {
        **{name: row[name] for name in ("repo", "worker", "source", "model")},
        **{name: _int(row.get(name)) for name in counters},
        "updated_at": epoch(row.get("updated_at")),
    }


def _budget(row: dict[str, Any], now: int) -> dict[str, Any] | str:
    concurrency, daily = (
        _int(row.get("concurrency_limit"), None),
        _int(row.get("daily_limit"), None),
    )
    if concurrency is None and daily is None:
        return "no_operator_limit"
    if (concurrency is not None and concurrency <= 0) or (daily is not None and daily < 0):
        return "limit_invalid"
    return {
        "repo": row["repo"],
        "source": row["source"],
        "concurrency_limit": concurrency,
        "daily_limit": daily,
        "updated_at": epoch(row.get("updated_at")),
    }


def _reservation(row: dict[str, Any], now: int) -> dict[str, Any] | str:
    expires = epoch(row.get("expires_at"))
    if expires is None or expires <= now:
        return "expired"
    claim = row["claim_id"]
    return {
        "claim_id": str(claim) if isinstance(claim, uuid.UUID | str) else claim,
        "repo": row["repo"],
        "worker": _text(row.get("worker")),
        "source": _text(row.get("source")),
        "expires_at": expires,
    }


def _retry(row: dict[str, Any], now: int) -> dict[str, Any]:
    return {
        "repo": row["repo"],
        "pr_number": _int(row["pr_number"], None),
        "head_sha": row["head_sha"],
        "stage": row["stage"],
        "attempts": _int(row.get("attempts")),
        "outcome": row["outcome"],
        "retry_after": epoch(row.get("retry_after")),
        "last_run_id": _int(row.get("last_run_id")),
        "updated_at": epoch(row.get("updated_at")),
    }


def _runtime_keys(row: dict[str, Any], now: int) -> dict[str, Any]:
    return {"repo": row["repo"], "run_id": _int(row["run_id"], None)}


def _daily(row: dict[str, Any], now: int) -> dict[str, Any]:
    day = row["day"]
    return {
        "repo": row["repo"],
        "day": day.isoformat() if isinstance(day, date) else day,
        **{name: row[name] for name in ("source", "model", "outcome_class")},
        **{name: _int(row.get(name)) for name in ("attempts", "timed_attempts", "duration_ms")},
    }


TABLE_MAPPERS: dict[str, Any] = {
    "factory_model_health": _health,
    "factory_worker_metrics": _metrics,
    "factory_provider_budget": _budget,
    "factory_provider_reservation": _reservation,
    "factory_pr_stage_retry": _retry,
    "factory_runtime_event_keys": _runtime_keys,
    "factory_model_attempt_daily": _daily,
}
