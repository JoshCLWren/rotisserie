"""Trusted read-only exports, backoff views and health resets on the libSQL store.

Same names and result shapes as the Neon operations. Provider account
cooldowns live in ``factory_backoff`` (kind ``provider``); the Neon
``factory_provider_budget`` columns they replace had no writer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import factory_turso_core as core
from factory_quota_reset import CAPACITY_OUTCOMES, policy
from factory_state_operations_backoff import (
    MAX_FIBONACCI_DELAY_SECONDS,
    effective_retry_at,
    legacy_capacity_backoff,
)
from factory_state_operations_core import REPO_RE
from factory_state_operations_quota import CREDIT_POOL_KEY
from factory_state_operations_reliability import RELIABILITY_WINDOWS, summarize_reliability
from factory_turso_backoff import active_capacity_cooldowns, state
from factory_turso_operations import roster


def _when(epoch: Any) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(int(epoch), UTC)


def _need(neon_style_optional: bool, message: str) -> bool:
    if core.enabled():
        return True
    if neon_style_optional and not core.required():
        return False
    raise RuntimeError(message)


def _exclusions(
    db: core.Store, repo: str, worker: str, requested_model: str
) -> dict[str, list[str]]:
    names = roster()
    override: tuple[str, str] | None = None
    if requested_model not in ("", "auto"):
        if not worker or worker not in names:
            raise ValueError("trusted worker required for exact-model override")
        source = names[worker][0]
        from factory_manual_model import validate_model_syntax

        validate_model_syntax(source, requested_model)
        if source == "opencode-free":
            bare = requested_model.split("/", 1)[1]
            health, account = db.reads(
                [
                    (
                        "SELECT status, last_outcome, cooldown_until FROM factory_model_health"
                        " WHERE repo = :repo AND source = :s AND model = :m",
                        {"repo": repo, "s": source, "m": bare},
                    ),
                    (
                        "SELECT 1 AS x FROM factory_backoff WHERE backoff_key = :k"
                        " AND outcome = 'provider_quota' AND expires_at > unixepoch()",
                        {"k": f"factory-provider-backoff-v1:{repo}:source:{source}"},
                    ),
                ]
            )
            now = datetime.now(UTC).timestamp()
            healthy = (
                bool(health)
                and health[0]["status"] == "healthy"
                and (
                    health[0]["last_outcome"] == "work_failure"
                    and (health[0]["cooldown_until"] is None or health[0]["cooldown_until"] <= now)
                )
            )
            if healthy and not account:
                override = (source, bare)
    health_rows, backoffs = db.reads(
        [
            (
                "SELECT source, model FROM factory_model_health"
                " INDEXED BY idx_factory_model_health_blocking WHERE repo = :repo"
                " AND status IN ('retired', 'unavailable', 'cooling')"
                " AND (status = 'retired' OR cooldown_until > unixepoch())",
                {"repo": repo},
            ),
            (
                "SELECT * FROM factory_backoff WHERE repo = :repo AND kind = 'model'"
                " AND expires_at > unixepoch()",
                {"repo": repo},
            ),
        ]
    )
    excluded: dict[str, list[str]] = {}
    for row in sorted(health_rows, key=lambda r: (r["source"], r["model"])):
        excluded.setdefault(str(row["source"]), []).append(str(row["model"]))
    now_dt = datetime.now(UTC)
    for row in backoffs:
        found = state(row)
        source, model = found.get("source"), found.get("model")
        if not isinstance(source, str) or not isinstance(model, str) or not model:
            continue
        if legacy_capacity_backoff(found):
            continue
        retry_at = effective_retry_at(found, _when(row["updated_at"]))
        if retry_at is not None and retry_at > now_dt:
            if (
                override == (source, model)
                and found.get("outcome") == "work_failure"
                and found.get("suspended") is not True
            ):
                continue
            if model not in excluded.setdefault(source, []):
                excluded[source].append(model)
    if worker and (not worker.isdecimal() or worker not in names):
        raise ValueError("invalid trusted worker for model exclusions")
    pools = sorted(active_capacity_cooldowns(db, repo)["pool"])
    if pools:
        excluded[CREDIT_POOL_KEY] = pools
    return excluded


def export_model_exclusions(
    repo: str, worker: str = "", requested_model: str = "auto"
) -> dict[str, list[str]]:
    from factory_fleet_switch import merge_exclusions

    if not _need(True, "the factory store is required for the model health snapshot"):
        return merge_exclusions({}, requested_model=requested_model)
    found = _exclusions(core.store(), repo, worker, requested_model)
    return merge_exclusions(found, requested_model=requested_model)


def export_candidate_health(repo: str) -> dict[str, Any]:
    """Compact advisory evidence for the unprivileged runner; never dispatch authority."""
    if not _need(True, "store health unavailable; cannot prove executable candidates"):
        return {"models": []}
    models, providers = core.store().reads(
        [
            (
                "SELECT source, model, status, last_outcome, cooldown_until, updated_at"
                " FROM factory_model_health WHERE repo = :repo",
                {"repo": repo},
            ),
            (
                "SELECT source, outcome, expires_at, updated_at FROM factory_backoff"
                " WHERE repo = :repo AND kind = 'provider' AND expires_at > unixepoch()",
                {"repo": repo},
            ),
        ]
    )
    return {
        "models": [
            {
                "source": r["source"],
                "model": r["model"],
                "status": r["status"],
                "outcome": r["last_outcome"],
                "updated": r["updated_at"],
                "until": r["cooldown_until"],
            }
            for r in models
        ],
        "providers": [
            {
                "source": r["source"],
                "outcome": r["outcome"],
                "updated": r["updated_at"],
                "until": r["expires_at"],
            }
            for r in providers
        ],
    }


def export_dashboard_retry(repo: str) -> dict[str, Any]:
    if not core.enabled():
        raise RuntimeError("Factory retry evidence is not connected")
    rows, reservations, retries = core.store().reads(
        [
            (
                "SELECT * FROM factory_backoff WHERE repo = :repo AND kind IN ('worker', 'model',"
                " 'provider') ORDER BY updated_at DESC LIMIT 750",
                {"repo": repo},
            ),
            (
                "SELECT worker, source, expires_at FROM factory_provider_reservation WHERE repo = :repo"
                " AND expires_at > unixepoch() ORDER BY expires_at LIMIT 300",
                {"repo": repo},
            ),
            (
                "SELECT pr_number, head_sha, stage, attempts, outcome, retry_after, last_run_id"
                " FROM factory_pr_stage_retry WHERE repo = :repo AND retry_after > unixepoch()"
                " ORDER BY retry_after LIMIT 150",
                {"repo": repo},
            ),
        ]
    )
    backoffs = []
    for row in rows:
        found = state(row)
        backoffs.append(
            {
                "kind": row["kind"],
                "worker": row["worker"] if row["kind"] == "worker" else "",
                "source": str(found.get("source") or ""),
                "model": str(found.get("model") or ""),
                "outcome": str(found.get("outcome") or ""),
                "streak": int(found.get("streak") or 0),
                "delay_seconds": int(found.get("delay_seconds") or 0),
                "suspended": found.get("suspended") is True,
                "next_retry_at": found.get("next_retry_at"),
                "until": _when(row["expires_at"]).isoformat()
                if row["kind"] == "provider"
                else None,
                "run_id": int(found.get("run_id") or 0),
                "updated_at": _when(row["updated_at"]).isoformat(),
            }
        )
    return {
        "backoffs": backoffs,
        "reservations": [
            {
                "worker": r["worker"],
                "source": r["source"],
                "expires_at": _when(r["expires_at"]).isoformat(),
            }
            for r in reservations
        ],
        "pr_retries": [
            {
                "pr": r["pr_number"],
                "head": r["head_sha"],
                "stage": r["stage"],
                "attempts": r["attempts"],
                "outcome": r["outcome"],
                "retry_after": _when(r["retry_after"]).isoformat(),
                "run_id": r["last_run_id"],
            }
            for r in retries
        ],
    }


def export_fleet_failover_sources(repo: str, worker: str) -> list[str]:
    """Sources a leased worker may fail over to without bypassing a capped provider."""
    from factory_fleet_switch import filter_sources
    from factory_provider_registry import PROVIDERS
    from factory_source_budget import load_budgets

    if not _need(True, "the factory store is required for fleet failover"):
        return []
    names = roster()
    if worker not in names:
        raise ValueError("unknown leased worker")
    assigned = names[worker][0]
    caps = load_budgets()
    cooling = active_capacity_cooldowns(core.store(), repo)["account"]
    sources = set(PROVIDERS) | {"opencode-free", "openrouter-free", "kilo-auto"}
    selected = {assigned} | {s for s in sources - {assigned} if s not in caps and s not in cooling}
    return filter_sources(sorted(selected))


def reset_model_health(repo: str, include_retired: bool = False) -> dict[str, int]:
    """Clear learned failure and cooldown state; keep delivery, lease and usage history."""
    if not REPO_RE.fullmatch(repo):
        raise ValueError("invalid repository")
    health, backoffs, providers, _ = core.store().batch(
        [
            (
                "DELETE FROM factory_model_health WHERE repo = :repo AND (:all = 1 OR status != 'retired')"
                " RETURNING 1 AS x",
                {"repo": repo, "all": int(include_retired)},
            ),
            (
                "DELETE FROM factory_backoff WHERE repo = :repo AND kind IN ('model', 'worker', 'pool')"
                " RETURNING 1 AS x",
                {"repo": repo},
            ),
            (
                "DELETE FROM factory_backoff WHERE repo = :repo AND kind = 'provider' RETURNING 1 AS x",
                {"repo": repo},
            ),
            (
                "INSERT INTO factory_repo_state (repo, health_reset_at) VALUES (:repo, unixepoch())"
                " ON CONFLICT(repo) DO UPDATE SET health_reset_at = excluded.health_reset_at",
                {"repo": repo},
            ),
        ]
    )
    return {
        "model_health_rows": len(health),
        "backoff_rows": len(backoffs) + len(providers),
        "provider_cooldowns": len(providers),
    }


def reset_quota_backoffs(repo: str) -> dict[str, int]:
    """Clear what quota and throttle refusals wrote; cap remaining streaks at one day."""
    if not REPO_RE.fullmatch(repo):
        raise ValueError("invalid repository")
    from factory_libsql import placeholders

    marks, params = placeholders("o", sorted(CAPACITY_OUTCOMES))
    removed, health, capacity, capped = core.store().batch(
        [
            (
                f"DELETE FROM factory_backoff WHERE repo = :repo AND kind IN ('model', 'worker')"
                f" AND outcome IN ({marks}) RETURNING 1 AS x",
                {**params, "repo": repo},
            ),
            (
                "UPDATE factory_model_health SET status = 'unknown', cooldown_until = NULL,"
                f" updated_at = unixepoch() WHERE repo = :repo AND status = 'cooling'"
                f" AND last_outcome IN ({marks}) RETURNING 1 AS x",
                {**params, "repo": repo},
            ),
            (
                "DELETE FROM factory_backoff WHERE repo = :repo AND kind IN ('provider', 'pool')"
                " RETURNING 1 AS x",
                {"repo": repo},
            ),
            (
                "UPDATE factory_backoff SET next_retry_at = updated_at + :cap WHERE repo = :repo"
                " AND kind IN ('model', 'worker') AND next_retry_at > updated_at + :cap RETURNING 1 AS x",
                {"repo": repo, "cap": MAX_FIBONACCI_DELAY_SECONDS},
            ),
        ]
    )
    return {
        "quota_backoff_rows": len(removed),
        "quota_health_rows": len(health),
        "capacity_cooldowns": len(capacity),
        "capped_fibonacci_rows": len(capped),
    }


def export_backoffs(repo: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    providers: dict[str, dict[str, Any]] = {}

    def bucket(source: str) -> dict[str, Any]:
        rule = policy(source)
        return providers.setdefault(
            source or "unknown",
            {
                "health": [],
                "fibonacci": [],
                "capacity": [],
                "reset_policy": f"{rule.period} {rule.scope}",
                "probe": rule.probe,
            },
        )

    db = core.store()
    health, fib = db.reads(
        [
            (
                "SELECT source, model, status, last_outcome, cooldown_until FROM factory_model_health"
                " INDEXED BY idx_factory_model_health_blocking WHERE repo = :repo"
                " AND status IN ('retired', 'unavailable', 'cooling')"
                " AND (status = 'retired' OR cooldown_until > unixepoch())",
                {"repo": repo},
            ),
            (
                "SELECT * FROM factory_backoff WHERE repo = :repo AND kind IN ('model', 'worker')"
                " AND expires_at > unixepoch()",
                {"repo": repo},
            ),
        ]
    )
    for row in health:
        until = _when(row["cooldown_until"])
        bucket(str(row["source"]))["health"].append(
            {
                "model": row["model"],
                "status": row["status"],
                "outcome": row["last_outcome"],
                "until": until.isoformat() if until else None,
            }
        )
    for row in fib:
        found = state(row)
        until = effective_retry_at(found, _when(row["updated_at"]))
        if until is None or until <= now:
            continue
        bucket(str(found.get("source") or ""))["fibonacci"].append(
            {
                "kind": row["kind"],
                "key": str(row["backoff_key"]).rsplit(":", 1)[-1],
                "model": found.get("model"),
                "outcome": found.get("outcome"),
                "streak": int(found.get("streak") or 0),
                "until": until.isoformat(),
                "quota_caused": found.get("outcome") in CAPACITY_OUTCOMES,
            }
        )
    for where, rows in active_capacity_cooldowns(db, repo).items():
        for source, found in rows.items():
            until = found.get("until")
            bucket(source)["capacity"].append(
                {
                    "scope": where,
                    "model": found.get("model"),
                    "outcome": found.get("outcome"),
                    "kind": found.get("kind"),
                    "reset_source": found.get("reset_source", "policy"),
                    "until": until.isoformat() if isinstance(until, datetime) else None,
                }
            )
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "providers": dict(sorted(providers.items())),
    }


def export_reliability(repo: str) -> dict[str, Any]:
    if not core.enabled():
        raise RuntimeError("Factory reliability requires the trusted factory store")
    seconds = {"24h": 86400, "7d": 604800}
    statements = [
        (
            "SELECT source, model, event_type, outcome_class,"
            " (detail ->> 'delivery_outcome') = 'persisted' AS delivered, count(*) AS n"
            " FROM factory_state_events WHERE repo = :repo AND subject_type = 'worker'"
            " AND event_type IN ('worker_completed', 'worker_idle', 'worker_failed')"
            " AND created_at > unixepoch() - :window GROUP BY 1, 2, 3, 4, 5",
            {"repo": repo, "window": seconds[name]},
        )
        for name in RELIABILITY_WINDOWS
    ]
    statements.append(
        (
            "SELECT source, model, status, last_outcome, cooldown_until"
            " FROM factory_model_health WHERE repo = :repo",
            {"repo": repo},
        )
    )
    results = core.store().reads(statements)
    rows = [
        {**row, "window": name}
        for name, found in zip(RELIABILITY_WINDOWS, results, strict=False)
        for row in found
    ]
    health = [{**row, "cooldown_until": _when(row["cooldown_until"])} for row in results[-1]]
    return summarize_reliability(rows, health, datetime.now(UTC))
