"""Project one trusted runtime outcome into health, backoff and metrics rows, once per run.

One batch. Every statement is fenced by ``NOT EXISTS`` on the run's key in
``factory_runtime_event_keys`` and the key is written last, so a duplicate or
replayed completion changes nothing, and a lost response can be resent.
The Fibonacci streak grows inside its UPSERT; the only value read beforehand
is the historical streak used when no backoff row exists yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from factory_quota_reset import CAPACITY_OUTCOMES
from factory_state_operations_backoff import (
    MODEL_COOLDOWNS,
    TRANSIENT,
    WORKER_RETRY_CLASSES,
    attributable_model_failure,
    historical_failure_streak,
    model_scoped_transient,
    validate_intermediate_model_failures,
)
from factory_turso_core import REPO_RE
from factory_state_operations_quota import capacity_hints
from factory_track_record_cooldown import STRIKE_WINDOW_SECONDS, WORK_OUTCOMES, Heat, delays_for
from factory_track_record_cooldown import heat, strike_delay
from factory_track_record_cooldown import UNAVAILABLE_WINDOW_SECONDS, recent_success_seconds
from factory_track_record_cooldown import unavailable_delay
from factory_turso_backoff import capacity_statements, clear_statement, streak_upsert
from factory_turso_operations import roster

HEALTH_UPSERT = (
    "INSERT INTO factory_model_health (repo, source, model, status, last_outcome,"
    " cooldown_until, last_verified_at, last_run_id, updated_at)"
    " SELECT :repo, :source, :model, :status, :outcome, :until, :verified, :run_id, unixepoch()"
    " WHERE {guard}"
    " ON CONFLICT(repo, source, model) DO UPDATE SET status = excluded.status,"
    " last_outcome = excluded.last_outcome, cooldown_until = excluded.cooldown_until,"
    " last_verified_at = CASE WHEN :retire = 1 THEN factory_model_health.last_verified_at"
    " ELSE excluded.last_verified_at END,"
    " last_run_id = excluded.last_run_id, updated_at = unixepoch()"
    " WHERE factory_model_health.last_run_id <= excluded.last_run_id"
    " AND (factory_model_health.status != 'retired' OR excluded.status = 'retired' AND :retire = 1)"
)


FRESH_STRIKE = f"factory_backoff.updated_at > unixepoch() - {STRIKE_WINDOW_SECONDS}"


def _epoch(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp())


def _history(
    db: Any,
    repo: str,
    run_id: int,
    source: str,
    model: str,
    worker: str | None,
    model_independent: bool = False,
) -> int:
    """Consecutive failures before this run, after the last manual health reset."""
    reset = (
        f"max(unixepoch() - {STRIKE_WINDOW_SECONDS}, coalesce((SELECT health_reset_at"
        " FROM factory_repo_state WHERE repo = :repo), 0))"
    )
    if worker is None:
        where, params = "source = :source AND model = :model", {"source": source, "model": model}
    else:
        where, params = "repo = :repo AND worker = :worker", {"worker": worker}
    rows = db.read(
        "SELECT event_type, detail FROM factory_state_events"
        f" WHERE {where} AND subject_type = 'worker'"
        " AND event_type IN ('worker_failed', 'worker_completed')"
        f" AND created_at > {reset} ORDER BY id DESC LIMIT 64",
        {**params, "repo": repo},
    )
    return historical_failure_streak(
        rows, run_id, source, model, model_only=worker is None, model_independent=model_independent
    )


def _kept(db: Any, repo: str, source: str, model: str, outcome: str) -> Heat | None:
    """Heat from the published record, or a recent completed run for an unavailable signal."""
    found = heat(source, model, outcome)
    if found is not None or outcome != "model_unavailable":
        return found
    window = recent_success_seconds()
    rows = db.read(
        "SELECT 1 AS x FROM factory_state_events WHERE repo = :repo AND source = :s"
        " AND model = :m AND subject_type = 'worker' AND event_type = 'worker_completed'"
        " AND created_at > unixepoch() - :window LIMIT 1",
        {"repo": repo, "s": source, "m": model, "window": window},
    )
    return Heat(1, 1, f"completed a run in the last {window // 3600}h") if rows else None


def _unavailable_wait(
    db: Any, repo: str, source: str, model: str, outcome: str, run_id: int, kept: Heat | None
) -> int:
    """Seconds an unavailable signal benches a model; long only for repeated strikes."""
    if outcome != "model_unavailable":
        return MODEL_COOLDOWNS[outcome]
    rows = db.read(
        "SELECT streak FROM factory_backoff WHERE backoff_key = :k AND outcome = :o"
        " AND run_id < :run AND updated_at > unixepoch() - :window",
        {
            "k": f"factory-model-backoff-v1:{repo}:source:{source}:model:{model}",
            "o": outcome,
            "run": run_id,
            "window": UNAVAILABLE_WINDOW_SECONDS,
        },
    )
    return unavailable_delay(int(rows[0]["streak"] or 0) + 1 if rows else 1, kept is not None)


def _evidence(raw: Any) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("evidence"), str):
            found[(str(entry.get("source") or ""), str(entry.get("model") or ""))] = entry[
                "evidence"
            ][:240]
    return found


def project_runtime(db: Any, repo: str, run_id: int, outcome: dict[str, Any]) -> bool:
    """Returns False when this run was already projected."""
    worker = str(outcome.get("worker") or "")
    names = roster()
    if worker not in names or not repo or not REPO_RE.fullmatch(repo):
        raise ValueError("untrusted worker or repository")
    source = names[worker][0]
    reported = str(outcome.get("source") or "")
    if reported and reported != source:
        if outcome.get("trusted_source_switch") is not True:
            raise ValueError("runtime source changed without trusted worker transition")
        from factory_provider_candidates import ADAPTERS

        if reported not in ADAPTERS:
            raise ValueError("runtime source is not an integrated provider")
        source = reported
    attributed = bool(reported and outcome.get("model"))
    model = str(outcome.get("model") or "unclassified")
    if len(model) > 180:
        raise ValueError("invalid model")
    classification = (
        "unconfigured"
        if outcome.get("configured") is False
        else str(outcome.get("outcome_class") or "unknown_failure")
    )
    proven = outcome.get("exact_model_proven") is True
    delivered = (
        outcome.get("persisted_work") is True
        and outcome.get("delivery_outcome") == "persisted"
        and str(outcome.get("delivery_pr") or "").isdecimal()
    )
    completed = classification == "success" or delivered
    failed = (
        classification not in {"success", "no_work", "no_capacity", "unconfigured"}
        and not delivered
    )
    observed = datetime.now(UTC)
    now = int(observed.timestamp())
    failures = validate_intermediate_model_failures(outcome.get("model_failures"), source)
    evidence = _evidence(outcome.get("model_failures"))
    hints = capacity_hints(outcome.get("quota_hints"))
    if db.read(
        "SELECT 1 AS x FROM factory_runtime_event_keys WHERE repo = :repo AND run_id = :run",
        {"repo": repo, "run": run_id},
    ):
        return False
    fresh = (
        "NOT EXISTS (SELECT 1 FROM factory_runtime_event_keys WHERE repo = :g_repo"
        " AND run_id = :g_run)"
    )
    guard: tuple[str, dict[str, Any]] = (fresh, {"g_repo": repo, "g_run": run_id})
    statements: list[tuple[str, dict[str, Any]]] = [
        (
            "INSERT INTO factory_worker_metrics (repo, worker, source, model, attempts,"
            " productive, completed, failures, last_run_id, updated_at)"
            " SELECT :repo, :worker, :source, :model, 1, :p, :c, :f, :run, unixepoch()"
            f" WHERE {fresh}"
            " ON CONFLICT(repo, worker, source, model) DO UPDATE SET"
            " attempts = factory_worker_metrics.attempts + 1,"
            " productive = factory_worker_metrics.productive + excluded.productive,"
            " completed = factory_worker_metrics.completed + excluded.completed,"
            " failures = factory_worker_metrics.failures + excluded.failures,"
            " last_run_id = max(factory_worker_metrics.last_run_id, excluded.last_run_id),"
            " updated_at = unixepoch()",
            {
                **guard[1],
                "repo": repo,
                "worker": worker,
                "source": source,
                "model": model,
                "p": int(delivered),
                "c": int(completed),
                "f": int(failed),
                "run": run_id,
            },
        )
    ]

    def health(
        src: str,
        mdl: str,
        status: str,
        last: str,
        until: datetime | None,
        verified: bool,
        retire: bool,
    ) -> None:
        statements.append(
            (
                HEALTH_UPSERT.format(guard=fresh),
                {
                    **guard[1],
                    "repo": repo,
                    "source": src,
                    "model": mdl,
                    "status": status,
                    "outcome": last,
                    "until": _epoch(until),
                    "verified": now if verified else None,
                    "run_id": run_id,
                    "retire": int(retire),
                },
            )
        )

    kept = _kept(db, repo, source, model, classification) if attributed and not delivered else None
    told = {"evidence": str(outcome.get("detail") or classification)[:240]}
    if classification == "model_retired_410":
        status, until = "retired", None
    elif classification in MODEL_COOLDOWNS:
        wait = _unavailable_wait(db, repo, source, model, classification, run_id, kept)
        status, until = "unavailable", observed + timedelta(seconds=wait)
    elif model_scoped_transient(source, classification):
        wait = strike_delay(classification, 1, kept is not None)
        status, until = "cooling", observed + timedelta(seconds=wait)
    elif proven:
        status, until = "healthy", None
    else:
        status, until = "unknown", None
    if attributed and (
        classification in MODEL_COOLDOWNS
        or model_scoped_transient(source, classification)
        or proven
    ):
        health(source, model, status, classification, until, proven, retire=False)
    if attributed and not delivered and classification in CAPACITY_OUTCOMES:
        statements += capacity_statements(
            repo,
            source,
            model,
            classification,
            "model",
            hints.get((source, model)),
            observed,
            run_id,
            guard,
        )
    for f_source, f_model, f_outcome, f_scope in failures:
        if f_outcome in CAPACITY_OUTCOMES:
            statements += capacity_statements(
                repo,
                f_source,
                f_model,
                f_outcome,
                f_scope,
                hints.get((f_source, f_model)),
                observed,
                run_id,
                guard,
            )
            continue
        f_heat = _kept(db, repo, f_source, f_model, f_outcome)
        f_told = evidence.get((f_source, f_model)) or evidence.get((source, f_model)) or f_outcome
        f_status = (
            "retired"
            if f_outcome == "model_retired_410"
            else "unavailable"
            if f_outcome in MODEL_COOLDOWNS
            else "cooling"
        )
        f_until = (
            None
            if f_status == "retired"
            else observed
            + timedelta(
                seconds=_unavailable_wait(db, repo, f_source, f_model, f_outcome, run_id, f_heat)
            )
            if f_outcome in MODEL_COOLDOWNS
            else observed + timedelta(seconds=strike_delay(f_outcome, 1, f_heat is not None))
        )
        health(f_source, f_model, f_status, f_outcome, f_until, False, retire=True)
        statements.append(
            streak_upsert(
                key=f"factory-model-backoff-v1:{repo}:source:{f_source}:model:{f_model}",
                repo=repo,
                kind="model",
                source=f_source,
                model=f_model,
                worker="",
                outcome=f_outcome,
                run_id=run_id,
                observed=now,
                fallback=_history(db, repo, run_id, f_source, f_model, None),
                keep_streak_sql=FRESH_STRIKE,
                guard=guard,
                detail={
                    "scope": f_scope,
                    "evidence": f_told,
                    **(f_heat.detail() if f_heat else {}),
                },
                delays=delays_for(f_outcome, f_heat is not None),
            )
        )
    if (
        attributed
        and proven
        and classification not in TRANSIENT
        and classification
        not in {
            "no_work",
            "no_capacity",
        }
    ):
        statements.append(
            clear_statement(
                f"factory-provider-backoff-v1:{repo}:source:{source}",
                run_id,
                guard,
            )
        )
    worker_key = f"factory-worker-backoff-v1:{repo}:worker:{worker}"
    if classification in WORKER_RETRY_CLASSES and not delivered:
        catalog_slot = names[worker][1] in ("", "@catalog")
        worker_heat = kept if classification in WORK_OUTCOMES else None
        statements.append(
            streak_upsert(
                key=worker_key,
                repo=repo,
                kind="worker",
                source=source,
                model=model,
                worker=worker,
                outcome=classification,
                run_id=run_id,
                observed=now,
                fallback=_history(db, repo, run_id, source, model, worker, catalog_slot),
                keep_streak_sql=(
                    f"{FRESH_STRIKE} AND factory_backoff.source = excluded.source"
                    " AND (:catalog = 1 OR factory_backoff.model = excluded.model)"
                ),
                guard=(fresh, {**guard[1], "catalog": int(catalog_slot)}),
                detail=worker_heat.detail() if worker_heat else None,
                delays=delays_for(classification, worker_heat is not None),
            )
        )
    elif delivered or (proven and classification in {"success", "no_work"}):
        statements.append(clear_statement(worker_key, run_id, guard))
    model_key = f"factory-model-backoff-v1:{repo}:source:{source}:model:{model}"
    if attributed and not delivered and attributable_model_failure(classification, proven):
        statements.append(
            streak_upsert(
                key=model_key,
                repo=repo,
                kind="model",
                source=source,
                model=model,
                worker="",
                outcome=classification,
                run_id=run_id,
                observed=now,
                fallback=_history(db, repo, run_id, source, model, None),
                keep_streak_sql=FRESH_STRIKE,
                guard=guard,
                detail={**told, **(kept.detail() if kept else {})},
                delays=delays_for(classification, kept is not None),
            )
        )
    elif attributed and (delivered or (proven and classification in {"success", "no_work"})):
        statements.append(clear_statement(model_key, run_id, guard))
    statements.append(
        (
            "INSERT OR IGNORE INTO factory_runtime_event_keys (repo, run_id) VALUES (:repo, :run)"
            " RETURNING run_id",
            {"repo": repo, "run": run_id},
        )
    )
    try:
        result = db.batch(statements)
    except Exception as error:
        from factory_libsql import StoreAmbiguous

        if not isinstance(error, StoreAmbiguous):
            raise
        result = db.batch(statements)
    return bool(result[-1])
