"""Backoff rows on the libSQL store: Fibonacci streaks and reset-timed capacity cooldowns.

A streak grows inside its own UPSERT (``streak = streak + 1``), so two runs
projected at once both count; the Neon path needed a per-key advisory lock
for that. The delay for the new streak comes from a fixed JSON table.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from factory_quota_reset import OVERSIZE, resolve, scope
from factory_state_operations_backoff import fibonacci_worker_delay
from factory_state_operations_quota import POOL_PREFIX, PROVIDER_PREFIX

STATE_TTL_SECONDS = 90 * 86400
MAX_STREAK_ROW = 40
DELAYS = json.dumps([fibonacci_worker_delay(n) for n in range(1, MAX_STREAK_ROW + 1)])
Guard = tuple[str, Mapping[str, Any]]


def _iso(epoch: Any) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(int(epoch), UTC).isoformat()


def _when(epoch: Any) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(int(epoch), UTC)


def state(row: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON state the Neon payload held, rebuilt from typed columns."""
    found = json.loads(row.get("detail") or "{}")
    found.update(
        {
            "source": row.get("source") or None,
            "model": row.get("model") or None,
            "outcome": row.get("outcome") or None,
            "streak": int(row.get("streak") or 0),
            "run_id": int(row.get("run_id") or 0),
        }
    )
    if row.get("next_retry_at") is not None:
        found["next_retry_at"] = _iso(row["next_retry_at"])
    return {key: value for key, value in found.items() if value is not None}


def record(row: Mapping[str, Any]) -> dict[str, Any]:
    """A Neon-shaped record, so the shared backoff policy functions read it unchanged."""
    return {
        "payload": [state(row)],
        "expires_at": _when(row["expires_at"]),
        "updated_at": _when(row["updated_at"]),
        "cache_key": row["backoff_key"],
    }


def streak_upsert(
    *,
    key: str,
    repo: str,
    kind: str,
    source: str,
    model: str,
    worker: str,
    outcome: str,
    run_id: int,
    observed: int,
    fallback: int,
    keep_streak_sql: str,
    guard: Guard,
    detail: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Grow one Fibonacci streak; an older run never overwrites a newer one.

    ``keep_streak_sql`` decides, against the stored row, whether the stored
    streak continues; otherwise the historical ``fallback`` restarts it.
    """
    condition, guard_params = guard
    new_streak = f"CASE WHEN {keep_streak_sql} THEN factory_backoff.streak ELSE :fallback END + 1"
    delay = "json_extract(:delays, '$[' || (min({s}, :max_row) - 1) || ']')"
    sql = (
        "INSERT INTO factory_backoff (backoff_key, repo, kind, source, model, worker, outcome,"
        " streak, run_id, next_retry_at, expires_at, updated_at, detail)"
        " SELECT :key, :repo, :kind, :source, :model, :worker, :outcome, :fallback + 1, :run_id,"
        f" :observed + {delay.format(s=':fallback + 1')},"
        f" max(unixepoch(), :observed + {delay.format(s=':fallback + 1')}) + :ttl,"
        " unixepoch(), :detail"
        f" WHERE {condition}"
        " ON CONFLICT(backoff_key) DO UPDATE SET source = excluded.source, model = excluded.model,"
        f" outcome = excluded.outcome, streak = {new_streak}, run_id = excluded.run_id,"
        f" next_retry_at = :observed + {delay.format(s='(' + new_streak + ')')},"
        f" expires_at = max(unixepoch(), :observed + {delay.format(s='(' + new_streak + ')')})"
        " + :ttl, updated_at = unixepoch(), detail = excluded.detail"
        " WHERE factory_backoff.run_id <= excluded.run_id"
    )
    params = {
        **guard_params,
        "key": key,
        "repo": repo,
        "kind": kind,
        "source": source,
        "model": model,
        "worker": worker,
        "outcome": outcome,
        "fallback": fallback,
        "run_id": run_id,
        "observed": observed,
        "delays": DELAYS,
        "max_row": MAX_STREAK_ROW,
        "ttl": STATE_TTL_SECONDS,
        "detail": json.dumps({"suspended": False, **(detail or {})}, sort_keys=True),
    }
    return sql, params


def clear_statement(key: str, run_id: int, guard: Guard) -> tuple[str, dict[str, Any]]:
    """Delete a backoff unless a newer run wrote it."""
    condition, guard_params = guard
    return (
        f"DELETE FROM factory_backoff WHERE backoff_key = :key AND run_id <= :run_id AND {condition}",
        {**guard_params, "key": key, "run_id": run_id},
    )


def capacity_statements(
    repo: str,
    source: str,
    model: str,
    outcome: str,
    marker_scope: str,
    hint: dict[str, object] | None,
    observed: datetime,
    run_id: int,
    guard: Guard,
) -> list[tuple[str, dict[str, Any]]]:
    """The one cooldown a capacity refusal earns; never shortens an active one."""
    reset = resolve(source, outcome, hint, observed)
    if reset.kind == OVERSIZE or reset.until is None:
        return []
    where = "pool" if marker_scope == "pool" else scope(source, reset.kind)
    until = int(reset.until.timestamp())
    condition, guard_params = guard
    statements: list[tuple[str, dict[str, Any]]] = []
    if where in ("account", "pool"):
        prefix = PROVIDER_PREFIX if where == "account" else POOL_PREFIX
        kind = "provider" if where == "account" else "pool"
        detail = {"kind": reset.kind, "scope": where, "reset_source": reset.reset_source}
        statements.append(
            (
                "INSERT INTO factory_backoff (backoff_key, repo, kind, source, model, outcome,"
                " run_id, next_retry_at, expires_at, updated_at, detail)"
                " SELECT :key, :repo, :kind, :source, :model, :outcome, :run_id, :until, :until,"
                f" unixepoch(), :detail WHERE {condition}"
                " ON CONFLICT(backoff_key) DO UPDATE SET source = excluded.source,"
                " model = excluded.model, outcome = excluded.outcome, run_id = excluded.run_id,"
                " next_retry_at = excluded.next_retry_at, expires_at = excluded.expires_at,"
                " updated_at = excluded.updated_at, detail = excluded.detail"
                " WHERE factory_backoff.expires_at <= excluded.expires_at",
                {
                    **guard_params,
                    "key": f"{prefix}:{repo}:source:{source}",
                    "repo": repo,
                    "kind": kind,
                    "source": source,
                    "model": model,
                    "outcome": outcome,
                    "run_id": run_id,
                    "until": until,
                    "detail": json.dumps(detail, sort_keys=True),
                },
            )
        )
    statements.append(
        (
            "INSERT INTO factory_model_health (repo, source, model, status, last_outcome,"
            " cooldown_until, last_run_id, updated_at)"
            " SELECT :repo, :source, :model, 'cooling', :outcome, :until, :run_id, unixepoch()"
            f" WHERE {condition}"
            " ON CONFLICT(repo, source, model) DO UPDATE SET status = excluded.status,"
            " last_outcome = excluded.last_outcome,"
            " cooldown_until = max(excluded.cooldown_until, CASE WHEN factory_model_health.status"
            " IN ('cooling', 'unavailable') THEN coalesce(factory_model_health.cooldown_until, 0)"
            " ELSE 0 END),"
            " last_run_id = excluded.last_run_id, updated_at = unixepoch()"
            " WHERE factory_model_health.last_run_id <= excluded.last_run_id"
            " AND factory_model_health.status != 'retired'",
            {
                **guard_params,
                "repo": repo,
                "source": source,
                "model": model,
                "outcome": outcome,
                "until": until,
                "run_id": run_id,
            },
        )
    )
    return statements


def active_capacity_cooldowns(db: Any, repo: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Active account and pool cooldowns: {"account"|"pool": {source: state}}."""
    found: dict[str, dict[str, dict[str, Any]]] = {"account": {}, "pool": {}}
    rows = db.read(
        "SELECT * FROM factory_backoff WHERE repo = :repo AND kind IN ('provider', 'pool')"
        " AND expires_at > unixepoch()",
        {"repo": repo},
    )
    for row in rows:
        bucket = "account" if row["kind"] == "provider" else "pool"
        found[bucket][str(row["source"])] = {**state(row), "until": _when(row["expires_at"])}
    return found
