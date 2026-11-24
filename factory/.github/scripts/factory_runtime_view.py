"""Trusted compact factory view. GitHub remains authoritative for mutations."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any
from factory_state_operations import _run, _export_candidate_health

EVENT_TYPES = ("worker_started", "worker_heartbeat", "worker_outcome_signal",
               "worker_completed", "worker_idle", "worker_failed")
TERMINAL = {"worker_completed", "worker_idle", "worker_failed"}

def _detail(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}

def _stamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value or "")

def project_events(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, dict[str, str]]]:
    """Do not infer terminal success from a heartbeat."""
    attempts: dict[str, dict[str, str]] = {}
    heartbeats: dict[str, dict[str, str]] = {}
    for row in rows:
        detail = _detail(row.get("detail"))
        worker = str(detail.get("worker") or "")
        kind = str(row.get("event_type") or "")
        if not worker.isdecimal() or int(worker) < 1 or kind not in EVENT_TYPES:
            continue
        updated = _stamp(detail.get("observed_at") or row.get("created_at"))
        try:
            datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            continue
        if kind in TERMINAL:
            target = attempts
            outcome = str(detail.get("outcome_class") or "")
            record = {
                "worker": worker,
                "model": str(detail.get("model") or ""),
                "source": str(detail.get("source") or ""),
                "attempt outcome": outcome or ("success" if kind == "worker_completed"
                    else "no_work" if kind == "worker_idle" else "unknown_failure"),
                "detail": str(detail.get("detail") or ""),
                "delivery_outcome": str(detail.get("delivery_outcome") or ""),
                "delivery_pr": str(detail.get("delivery_pr") or ""),
                "persisted_work": ("true" if detail.get("persisted_work") is True
                                   else "false" if detail.get("persisted_work") is False
                                   else ""),
                "run": str(detail.get("run_id") or ""),
                "updated": updated,
            }
        else:
            target = heartbeats
            record = {
                "worker": worker,
                "run": str(detail.get("run_id") or ""),
                "outcome": str(detail.get("status") or
                    ("running" if kind == "worker_started" else "finished")),
                "updated": updated,
            }
        if updated > target.get(worker, {}).get("updated", ""):
            target[worker] = record
    return {"attempts": attempts, "heartbeats": heartbeats}

async def _export_view(conn: Any, repo: str) -> dict[str, Any]:
    # Fixed-size projection: one event per worker/type, no historical snapshots.
    rows = await conn.fetch(
        """SELECT DISTINCT ON (detail->>'worker', event_type)
                   event_type, detail, created_at
             FROM public.factory_state_events
             WHERE source='github-actions' AND subject_type='worker'
               AND detail->>'repo'=$1 AND event_type=ANY($2::text[])
               AND created_at>now()-interval '7 days'
             ORDER BY detail->>'worker', event_type, created_at DESC""",
        repo, list(EVENT_TYPES),
    )
    view: dict[str, Any] = project_events([dict(row) for row in rows])
    view["health"] = await _export_candidate_health(conn, repo)
    return view

def export_view(repo: str) -> dict[str, Any]:
    return _run(_export_view, repo)

async def _run_workers(conn: Any, repo: str, ids: list[str]) -> dict[str, str]:
    if not ids or any(not value.isdecimal() for value in ids):
        return {}
    rows = await conn.fetch(
        """SELECT DISTINCT ON (detail->>'run_id')
                  detail->>'run_id' AS run_id, detail->>'worker' AS worker
           FROM public.factory_state_events
           WHERE source='github-actions' AND subject_type='worker'
             AND detail->>'repo'=$1 AND detail->>'run_id'=ANY($2::text[])
           ORDER BY detail->>'run_id', id DESC""",
        repo, ids,
    )
    return {str(row["run_id"]): str(row["worker"])
            for row in rows if str(row["worker"]).isdecimal()}

def run_workers(repo: str, ids: list[str]) -> dict[str, str]:
    return _run(_run_workers, repo, ids) if ids else {}
