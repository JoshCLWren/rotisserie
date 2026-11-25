"""Let an operator-forced run use its requested model past the factory's own exclusions.

``baste run --force`` and ``baste unstick`` dispatch with an operator claim.
The worker then skips the factory's learned health and backoff exclusions for
the one requested model. Real capacity signals still apply: an active quota or
throttle cooldown on the model, a provider account cooldown, a credit pool
cooldown (its own key), a retired model, and the fleet switches applied after
this step. The live catalog check in ``factory_manual_model`` still runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from factory_health_cooldown import end_sql
from factory_quota_reset import CAPACITY_OUTCOMES


def _epoch(value: Any) -> str:
    if value is None:
        return "no end time"
    return datetime.fromtimestamp(int(value), UTC).strftime("%Y-%m-%d %H:%M UTC")


def release(
    db: Any, repo: str, source: str, requested_model: str, excluded: dict[str, list[str]]
) -> str:
    """Drop the requested model from ``excluded`` unless capacity holds it; return a note."""
    model = requested_model.split("/", 1)[-1]
    if model not in excluded.get(source, []):
        return ""
    health, backoffs, account = db.reads(
        [
            (
                f"SELECT status, last_outcome, {end_sql()} AS cooldown_until"
                " FROM factory_model_health WHERE repo = :repo AND source = :s AND model = :m",
                {"repo": repo, "s": source, "m": model},
            ),
            (
                "SELECT outcome, streak, next_retry_at FROM factory_backoff WHERE repo = :repo"
                " AND kind = 'model' AND source = :s AND model = :m AND expires_at > unixepoch()",
                {"repo": repo, "s": source, "m": model},
            ),
            (
                "SELECT outcome FROM factory_backoff WHERE repo = :repo AND kind = 'provider'"
                " AND source = :s AND expires_at > unixepoch()",
                {"repo": repo, "s": source},
            ),
        ]
    )
    now = datetime.now(UTC).timestamp()
    row = health[0] if health else None
    cooling = row is not None and (row["cooldown_until"] or 0) > now
    if account:
        return f"forced run kept {model} excluded: provider account {source} is cooling"
    if row is not None and row["status"] == "retired":
        return f"forced run kept {model} excluded: the provider retired it"
    until = _epoch(row["cooldown_until"]) if row is not None else ""
    if cooling and row["last_outcome"] in CAPACITY_OUTCOMES:
        return f"forced run kept {model} excluded: {row['last_outcome']} until {until}"
    reasons = []
    if cooling:
        reasons.append(f"health {row['status']} / {row['last_outcome']} until {until}")
    for found in backoffs:
        if (found["next_retry_at"] or 0) > now and found["outcome"] not in CAPACITY_OUTCOMES:
            retry = _epoch(found["next_retry_at"])
            reasons.append(f"backoff streak {found['streak']} ({found['outcome']}) until {retry}")
    excluded[source] = [name for name in excluded[source] if name != model]
    if not excluded[source]:
        del excluded[source]
    why = "; ".join(reasons) or "a factory exclusion"
    return f"forced past health exclusion: {source}/{model} ({why})"


def forced_exclusions(
    repo: str, worker: str, requested_model: str
) -> tuple[dict[str, list[str]], str]:
    """The worker's exclusion snapshot for an operator-forced run, and what it skipped."""
    import factory_turso_core as core
    from factory_fleet_switch import merge_exclusions
    from factory_turso_exports import _exclusions
    from factory_turso_operations import roster

    if requested_model in ("", "auto") or not core.enabled():
        from factory_turso_exports import export_model_exclusions

        return export_model_exclusions(repo, worker, requested_model), ""
    db = core.store()
    found = _exclusions(db, repo, worker, requested_model)
    note = release(db, repo, roster()[worker][0], requested_model, found)
    return merge_exclusions(found, requested_model=requested_model), note
