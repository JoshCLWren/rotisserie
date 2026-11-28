"""Idempotent trusted events, the compact runtime view and event retention on the libSQL store.

``event_id`` is UNIQUE, so ingestion is ``INSERT OR IGNORE`` with no lock and
no prior read: concurrent or repeated collectors insert each event once.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import factory_turso_core as core
from factory_libsql import placeholders
from factory_state_ledger_core import WORKER_RE
from factory_turso_leases import bind_run_statements, renewal_statements
from factory_work_policy import FIXED_LEASE_TTL_SECONDS

ORIGIN = "github-actions"
NO_REVIEWER = ("No reviewer model:", "Every available model authored this PR")
HEAD_RE = re.compile(r"[0-9a-f]{40}")
LIFECYCLE_STATUSES = {
    "started": "worker_started",
    "finished": "worker_outcome_signal",
    "failure": "worker_outcome_signal",
}
CHUNK = 40
RAW_DAYS = 120
BATCH = 1000
LIFECYCLE_DAYS = 30
RETIRED_LIFECYCLE = (
    "target_changed",
    "worker_assigned",
    "worker_started",
    "worker_heartbeat",
    "worker_outcome_signal",
    "worker_released",
    "worker_failed",
    "worker_completed",
    "worker_idle",
)


def _insert(repo: str, entry: dict[str, Any]) -> core.Statement:
    return core.event_insert(
        repo,
        ORIGIN,
        str(entry["event_type"]),
        entry,
        subject_type="worker",
        subject_number=int(entry["worker"]),
    )


def record_events(repo: str, entries: list[dict[str, Any]]) -> int:
    """Insert each event once; a newly recorded ``worker_started`` renews that worker's claim."""
    if not core.enabled() or not entries:
        return 0
    db = core.store()
    inserted = 0
    started: list[str] = []
    bound: list[core.Statement] = []
    for offset in range(0, len(entries), CHUNK):
        chunk = entries[offset : offset + CHUNK]
        results = db.batch([_insert(repo, entry) for entry in chunk])
        for entry, rows in zip(chunk, results, strict=True):
            if rows:
                inserted += 1
                if entry["event_type"] == "worker_started" and entry.get("claim_id"):
                    claim, run = str(entry["claim_id"]), str(entry["run_id"])
                    bound += bind_run_statements(repo, claim, run, FIXED_LEASE_TTL_SECONDS)
                elif entry["event_type"] == "worker_started":
                    started.append(str(entry["worker"]))
    if started:
        bound += renewal_statements(repo, started, FIXED_LEASE_TTL_SECONDS)
    if bound:
        db.batch(bound)
    return inserted


def record_lifecycle(
    repo: str, run_id: str, worker: str, status: str, claim_id: str | None = None
) -> int:
    if not WORKER_RE.fullmatch(worker) or not run_id.isdecimal():
        raise ValueError("invalid trusted lifecycle identity")
    if status not in LIFECYCLE_STATUSES:
        raise ValueError("invalid trusted lifecycle status")
    entry = {
        "event_type": LIFECYCLE_STATUSES[status],
        "event_id": f"lifecycle:{run_id}:{status}",
        "worker": worker,
        "run_id": run_id,
        "status": "running" if status == "started" else status,
        "observed_at": datetime.now(UTC).isoformat(),
    }
    if claim_id:
        entry["claim_id"] = claim_id
    return record_events(repo, [entry])


def record_runtime_outcome(
    repo: str, run_id: str, outcome: dict[str, Any], job_log: str | None = None
) -> int:
    """Terminal worker event, per-model attempts and the once-per-run health projection."""
    worker = str(outcome.get("worker") or "")
    if not WORKER_RE.fullmatch(worker) or not run_id.isdecimal():
        raise ValueError("invalid trusted runtime outcome identity")
    status = str(outcome.get("outcome_class") or "unknown_failure")
    delivered = (
        outcome.get("delivery_outcome") == "persisted"
        and outcome.get("persisted_work") is True
        and str(outcome.get("delivery_pr") or "").isdecimal()
    )
    event_type = (
        "worker_completed"
        if status == "success" or delivered
        else "worker_idle"
        if status in {"no_work", "no_capacity"}
        else "worker_failed"
    )
    entries = [
        {
            "event_type": event_type,
            "event_id": f"runtime:{run_id}",
            "worker": worker,
            "run_id": run_id,
            "source": outcome.get("source"),
            "model": outcome.get("model"),
            "exact_model_proven": outcome.get("exact_model_proven") is True,
            "outcome_class": status,
            "delivery_outcome": "persisted" if delivered else "none",
            "delivery_pr": outcome.get("delivery_pr") if delivered else None,
            "selected_kind": outcome.get("selected_kind"),
            "selected_number": outcome.get("selected_number"),
            "action_kind": outcome.get("action_kind"),
            "selection_reason": outcome.get("selection_reason"),
            "persisted_work": bool(outcome.get("persisted_work")),
            "model_failures": outcome.get("model_failures") or [],
            "trusted_source_switch": outcome.get("trusted_source_switch") is True,
            "observed_at": datetime.now(UTC).isoformat(),
        }
    ]
    if status == "no_capacity" and str(outcome.get("detail") or "").startswith(NO_REVIEWER):
        head = str(outcome.get("assigned_head") or "")
        number = str(outcome.get("assigned_number") or outcome.get("selected_number") or "")
        entries[0] |= {
            "no_reviewer": True,
            "head": head if HEAD_RE.fullmatch(head) else "",
            "no_reviewer_pr": int(number) if number.isdecimal() else None,
        }
    if job_log is not None:
        from factory_model_attempt_events import model_attempt_events

        entries.extend(model_attempt_events(run_id, outcome, job_log))
    inserted = record_events(repo, entries)
    if core.enabled():
        from factory_turso_runtime import project_runtime

        project_runtime(core.store(), repo, int(run_id), outcome)
    return inserted


def recorded_runtime_runs(repo: str, run_ids: list[str]) -> set[str]:
    if not core.enabled() or not run_ids:
        return set()
    if any(not run_id.isdecimal() for run_id in run_ids):
        raise ValueError("invalid run ID")
    marks, params = placeholders("e", [f"runtime:{run_id}" for run_id in run_ids])
    rows = core.store().read(
        f"SELECT run_id FROM factory_state_events WHERE event_id IN ({marks}) AND +repo = :repo",
        {**params, "repo": repo},
    )
    return {str(row["run_id"]) for row in rows}


def _latest_sql() -> str:
    """The newest events of each worker, read through the two per-worker partial indexes.

    The view keeps one terminal and one heartbeat record per worker, so reading
    seven days of every worker's events (about 8,200 rows on Nov 27) is not needed.
    Two rows per group cover events stamped in the same second.
    """
    # SQLite uses a partial index only when the IN list is spelled exactly as in
    # its WHERE clause (factory-state.sqlite.sql), so these lists are copied verbatim.
    quoted = [
        "('worker_completed', 'worker_idle', 'worker_failed')",
        "('worker_started', 'worker_heartbeat', 'worker_outcome_signal')",
    ]
    walk = " UNION ".join(
        f"SELECT * FROM (WITH RECURSIVE w{n}(worker) AS ("
        f"SELECT (SELECT min(worker) FROM factory_state_events WHERE repo = :repo"
        f" AND worker > '' AND event_type IN {types})"
        f" UNION ALL SELECT (SELECT min(worker) FROM factory_state_events WHERE repo = :repo"
        f" AND worker > w{n}.worker AND event_type IN {types}) FROM w{n}"
        f" WHERE w{n}.worker IS NOT NULL) SELECT worker FROM w{n} WHERE worker IS NOT NULL)"
        for n, types in enumerate(quoted)
    )
    newest = " UNION ALL ".join(
        "SELECT * FROM (SELECT rowid FROM factory_state_events WHERE repo = :repo"
        f" AND worker = workers.worker AND created_at > unixepoch() - 604800"
        f" AND event_type IN {types} AND origin = :origin AND subject_type = 'worker'"
        " ORDER BY created_at DESC LIMIT 2)"
        for types in quoted
    )
    return (
        f"WITH workers(worker) AS MATERIALIZED ({walk})"
        " SELECT e.event_type, e.detail, e.created_at FROM workers"
        f" JOIN factory_state_events e ON e.rowid IN ({newest})"
    )


def export_view(repo: str) -> dict[str, Any]:
    """Latest event per worker and lifecycle type over seven days, plus candidate health."""
    from factory_runtime_view import project_events
    from factory_turso_exports import export_candidate_health

    rows = core.store().read(_latest_sql(), {"repo": repo, "origin": ORIGIN})
    for row in rows:
        row["created_at"] = datetime.fromtimestamp(int(row["created_at"]), UTC)
    view: dict[str, Any] = project_events(rows)
    view["health"] = export_candidate_health(repo)
    return view


def run_workers(repo: str, ids: list[str]) -> dict[str, str]:
    if not ids or any(not value.isdecimal() for value in ids):
        return {}
    marks, params = placeholders("r", ids)
    rows = core.store().read(
        "SELECT run_id, worker FROM factory_state_events"
        f" WHERE repo = :repo AND run_id IN ({marks}) AND event_type <> 'model_attempt'"
        " AND origin = :origin"
        " AND subject_type = 'worker' ORDER BY id DESC",
        {**params, "repo": repo, "origin": ORIGIN},
    )
    found: dict[str, str] = {}
    for row in rows:
        if str(row["worker"] or "").isdecimal():
            found.setdefault(str(row["run_id"]), str(row["worker"]))
    return found


def rollup_statements(repo: str, bound: int, days: int) -> list[core.Statement]:
    """Count expired raw attempts per UTC day, then delete exactly those rows, in one batch."""
    where = (
        "repo = :repo AND event_type = 'model_attempt' AND origin = 'github-actions'"
        " AND created_at < unixepoch() - :days * 86400 AND id <= :bound"
    )
    params = {"repo": repo, "days": days, "bound": bound}
    duration = "CASE WHEN json_type(detail, '$.duration_ms') IN ('integer', 'real')"
    return [
        (
            "INSERT INTO factory_model_attempt_daily (repo, day, source, model, outcome_class,"
            " attempts, timed_attempts, duration_ms)"
            " SELECT :repo, date(created_at, 'unixepoch'), coalesce(source, ''),"
            " coalesce(model, ''), coalesce(outcome_class, 'unknown'), count(*),"
            f" sum({duration} THEN 1 ELSE 0 END),"
            f" sum({duration} THEN CAST(detail ->> 'duration_ms' AS INTEGER) ELSE 0 END)"
            f" FROM factory_state_events WHERE {where} GROUP BY 2, 3, 4, 5"
            " ON CONFLICT(repo, day, source, model, outcome_class) DO UPDATE SET"
            " attempts = attempts + excluded.attempts,"
            " timed_attempts = timed_attempts + excluded.timed_attempts,"
            " duration_ms = duration_ms + excluded.duration_ms",
            params,
        ),
        (f"DELETE FROM factory_state_events WHERE {where} RETURNING 1 AS x", params),
    ]


def rollup_model_attempts(repo: str, *, days: int = RAW_DAYS, batch: int = BATCH) -> int:
    if days < 30 or not 1 <= batch <= 1000:
        raise ValueError("invalid retention scope")
    db = core.store()
    rows = db.read(
        "SELECT max(id) AS bound FROM (SELECT id FROM factory_state_events WHERE repo = :repo"
        " AND event_type = 'model_attempt' AND created_at < unixepoch() - :days * 86400"
        " ORDER BY id LIMIT :batch)",
        {"repo": repo, "days": days, "batch": batch},
    )
    if not rows or rows[0]["bound"] is None:
        return 0
    return len(db.batch(rollup_statements(repo, int(rows[0]["bound"]), days))[1])


def prune_lifecycle(repo: str, *, batch: int = BATCH) -> int:
    """Delete this repository's lifecycle events older than 30 days, in bounded batches."""
    marks, params = placeholders("t", RETIRED_LIFECYCLE)
    (rows,) = core.store().batch(
        [
            (
                "DELETE FROM factory_state_events WHERE id IN (SELECT id FROM factory_state_events"
                f" WHERE repo = :repo AND event_type IN ({marks})"
                " AND created_at < unixepoch() - :days * 86400 ORDER BY id LIMIT :batch)"
                " RETURNING 1 AS x",
                {**params, "repo": repo, "days": LIFECYCLE_DAYS, "batch": batch},
            )
        ]
    )
    return len(rows)


def history_rows(
    repo: str, days: int, cap: int, event_types: Sequence[str]
) -> list[dict[str, Any]]:
    """The estimate history rows (``estimate_neon.QUERY``) in SQLite form."""
    marks, params = placeholders("t", event_types)
    code = "coalesce(detail ->> 'agent_exit_code', '')"
    ms = "coalesce(detail ->> 'duration_ms', '')"
    ran = (
        f"(event_type <> 'model_attempt'"
        f" OR (({code} GLOB '[0-9]*' OR {code} GLOB '-[0-9]*')"
        f" AND substr({code}, 2) NOT GLOB '*[^0-9]*')"
        f" OR ({ms} GLOB '[1-9]*' AND {ms} NOT GLOB '*[^0-9]*'))"
    )
    rows = core.store().read(
        "WITH recent AS (SELECT event_type, created_at, subject_number, detail,"
        f" {ran} AS ran FROM factory_state_events WHERE repo = :repo"
        f" AND created_at >= unixepoch() - :days * 86400 AND event_type IN ({marks}))"
        " SELECT event_type, created_at, subject_number, detail ->> 'run_id' AS run_id,"
        " detail ->> 'targets' AS targets, detail ->> 'selected_number' AS selected_number,"
        " detail ->> 'outcome_class' AS outcome_class, detail ->> 'source' AS source,"
        " detail ->> 'model' AS model, detail ->> 'duration_ms' AS duration_ms,"
        " detail ->> 'agent_exit_code' AS agent_exit_code, detail ->> 'client' AS client,"
        " 1 AS row_count FROM recent WHERE ran"
        " UNION ALL SELECT 'model_attempt', max(created_at), NULL, detail ->> 'run_id', NULL,"
        " detail ->> 'selected_number', detail ->> 'outcome_class', NULL, NULL, NULL, NULL,"
        " NULL, count(*) FROM recent WHERE NOT ran"
        " GROUP BY detail ->> 'run_id', detail ->> 'selected_number',"
        " detail ->> 'outcome_class' ORDER BY created_at DESC LIMIT :cap",
        {**params, "repo": repo, "days": days, "cap": cap},
    )
    for row in rows:
        row["created_at"] = datetime.fromtimestamp(int(row["created_at"]), UTC)
        if row["targets"] is not None:
            row["targets"] = json.loads(row["targets"])
    return rows
