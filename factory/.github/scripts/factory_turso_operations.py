"""Dispatch eligibility, daily usage, PR retry cooldowns and worker ranking on the libSQL store.

Same public names and results as ``factory_state_operations``. Eligibility is
a fresh read before the acquisition batch, as on Neon, where model health and
backoff writers never took the repository lock either; the authorizing
capacity checks run inside the batch itself (``factory_turso_leases``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import factory_turso_core as core
from factory_libsql import placeholders
from factory_state_operations_backoff import MODEL_RETRY_CLASSES, worker_backoff_reason
from factory_state_operations_capacity import (
    REVIEW_ABORT_LIMIT,
    REVIEW_ABORT_LOOKBACK_SECONDS,
    REVIEW_ABORT_MAX_COOLDOWN_SECONDS,
)
from factory_state_operations_core import HEAD_RE
from factory_turso_backoff import record


def roster() -> dict[str, tuple[str, str]]:
    from factory_worker_slots import all_identities

    return {worker: (source, "") for worker, source in all_identities().items()}


def roster_source(worker: str) -> str:
    return roster().get(worker, ("", ""))[0]


def _when(epoch: Any) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(int(epoch), UTC)


def eligibility(db: core.Store, repo: str, workers: list[str]) -> tuple[list[str], list[str]]:
    names = roster()
    pairs = sorted({names[w] for w in workers if w in names})
    keys = [f"factory-worker-backoff-v1:{repo}:worker:{w}" for w in workers]
    keys += [f"factory-model-backoff-v1:{repo}:source:{s}:model:{m}" for s, m in pairs]
    keys += [f"factory-provider-backoff-v1:{repo}:source:{s}" for s in {s for s, _ in pairs}]
    key_marks, key_params = placeholders("k", keys)
    results = db.reads(
        [
            (
                "SELECT source, model, status, cooldown_until FROM factory_model_health"
                " WHERE repo = :repo AND source = :s AND model = :m",
                {"repo": repo, "s": source, "m": model},
            )
            for source, model in pairs
        ]
        + [
            (
                f"SELECT * FROM factory_backoff WHERE backoff_key IN ({key_marks or 'NULL'})"
                " AND expires_at > unixepoch()",
                key_params,
            )
        ]
    )
    health_rows = [row for rows in results[:-1] for row in rows]
    backoff_rows = results[-1]
    models = {(r["source"], r["model"]): r for r in health_rows}
    now = datetime.now(UTC)
    worker_backoffs: dict[str, dict[str, Any]] = {}
    model_backoffs: dict[tuple[str, str], dict[str, Any]] = {}
    provider_cooling: set[str] = set()
    for row in backoff_rows:
        if row["kind"] == "worker":
            worker_backoffs[str(row["worker"])] = record(row)
        elif row["kind"] == "model" and row["source"] and row["model"]:
            model_backoffs[(row["source"], row["model"])] = record(row)
        elif row["kind"] == "provider" and row["outcome"] in {
            "provider_quota",
            "provider_failure",
            "provider_throttle",
        }:
            provider_cooling.add(str(row["source"]))
    kept, rejected = [], []
    for worker in workers:
        pair = names.get(worker)
        if pair is None:
            rejected.append(f"{worker}: missing trusted roster entry")
            continue
        source, model = pair
        health = models.get(pair)
        own = worker_backoffs.get(worker)
        retry_reason = worker_backoff_reason(own, source, model, now)
        if own and own["payload"][0].get("outcome") in (
            MODEL_RETRY_CLASSES | {"model_retired_410"}
        ):
            retry_reason = None
        model_retry = worker_backoff_reason(model_backoffs.get(pair), source, model, now)
        until = _when(health["cooldown_until"]) if health else None
        if source in provider_cooling:
            rejected.append(f"{worker}: provider account {source} cooling")
        elif retry_reason:
            rejected.append(f"{worker}: {retry_reason}")
        elif model_retry:
            rejected.append(f"{worker}: source/model {model} {model_retry}")
        elif health and (
            health["status"] == "retired"
            or (health["status"] in {"unavailable", "cooling"} and until and until > now)
        ):
            rejected.append(f"{worker}: model {model} {health['status']}")
        else:
            kept.append(worker)
    return kept, rejected


def eligible_source(db: core.Store, repo: str, worker: str) -> str | None:
    """The worker's provider when it may run now; the batch then enforces capacity."""
    source = roster().get(worker)
    if source is None:
        return None
    kept, _ = eligibility(db, repo, [worker])
    return source[0] if kept else None


def filter_workers(repo: str, workers: list[str]) -> tuple[list[str], list[str]]:
    if not core.enabled():
        return workers, ["factory store unavailable; existing lease checks still required"]
    kept, rejected = eligibility(core.store(), repo, workers)
    if kept:
        from factory_state_operations_slots import drop_exhausted_slots
        from factory_turso_exports import export_model_exclusions

        try:
            kept, empty = drop_exhausted_slots(repo, kept, export_model_exclusions(repo))
            rejected += empty
        except Exception as exc:  # noqa: BLE001 - the slot check is advisory; fail open
            rejected.append(f"catalog slot check skipped ({type(exc).__name__})")
    return kept, rejected


def daily_usage(repo: str, sources: list[str]) -> dict[str, int] | None:
    if not core.enabled():
        return None
    rows = core.store().read(
        "SELECT source, count(*) AS n FROM factory_state_events WHERE repo = :repo"
        " AND event_type = 'worker_assigned' AND created_at >= unixepoch('now', 'start of day')"
        " GROUP BY source",
        {"repo": repo},
    )
    counts = dict.fromkeys(sources, 0)
    for row in rows:
        if row["source"] in counts:
            counts[row["source"]] = int(row["n"])
    return counts


def _review_churn(db: core.Store, repo: str, heads: dict[int, tuple[str, str]]) -> set[int]:
    review = {number for number, (_, stage) in heads.items() if stage == "factory:review"}
    if not review:
        return set()
    from factory_settings import integer

    window = integer("FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS", 1800)
    counts: dict[int, int] = {}
    latest: dict[int, int] = {}
    for row in db.read(
        "SELECT detail ->> 'targets' AS targets, created_at FROM factory_state_events"
        " WHERE repo = :repo AND event_type = 'worker_released'"
        " AND created_at > unixepoch() - :lookback"
        " AND substr(detail ->> 'reason', 1, 17) = 'pre-session-abort'",
        {"repo": repo, "lookback": REVIEW_ABORT_LOOKBACK_SECONDS},
    ):
        for item in json.loads(row["targets"] or "[]"):
            number = int(item) if str(item).isdecimal() else None
            if number in review:
                counts[number] = counts.get(number, 0) + 1
                latest[number] = max(latest.get(number, 0), int(row["created_at"]))
    now = datetime.now(UTC).timestamp()
    return {
        number
        for number, count in counts.items()
        if count >= REVIEW_ABORT_LIMIT
        and now < latest[number] + min(window * (count - 1), REVIEW_ABORT_MAX_COOLDOWN_SECONDS)
    }


def cooling_prs(repo: str, heads: dict[int, tuple[str, str]]) -> set[int]:
    if not core.enabled() or not heads:
        return set()
    db = core.store()
    rows = db.read(
        "SELECT pr_number, head_sha, stage FROM factory_pr_stage_retry"
        " WHERE repo = :repo AND retry_after > unixepoch()",
        {"repo": repo},
    )
    cooling = {
        int(r["pr_number"])
        for r in rows
        if heads.get(int(r["pr_number"])) == (r["head_sha"], r["stage"])
    }
    return cooling | _review_churn(db, repo, heads)


def ingest_check(repo: str, event: dict[str, Any]) -> int:
    """Record failed-review cooldowns for the exact head; a replayed run counts once."""
    if not core.enabled():
        raise RuntimeError("review cooldown ingestion requires the factory store")
    run = event.get("workflow_run") or {}
    sha, name = str(run.get("head_sha") or ""), str(run.get("name") or "")
    try:
        number = int(run.get("id") or 0)
    except TypeError, ValueError:
        return 0
    if (
        not HEAD_RE.fullmatch(sha)
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or name not in {"PR-Agent Advisory Review", "Local Ollama Review"}
        or number <= 0
    ):
        return 0
    statements = []
    for pr in run.get("pull_requests") or []:
        pr_num = pr.get("number") if isinstance(pr, dict) else None
        if not isinstance(pr_num, int) or isinstance(pr_num, bool) or pr_num <= 0:
            continue
        statements.append(
            (
                "INSERT INTO factory_pr_stage_retry (repo, pr_number, head_sha, stage, attempts,"
                " outcome, retry_after, last_run_id, updated_at)"
                " VALUES (:repo, :pr, :sha, 'factory:review', 1, 'review_transport_failure',"
                " unixepoch() + 300, :run, unixepoch())"
                " ON CONFLICT(repo, pr_number, head_sha, stage) DO UPDATE SET"
                " attempts = factory_pr_stage_retry.attempts + 1, outcome = excluded.outcome,"
                " retry_after = excluded.retry_after, last_run_id = excluded.last_run_id,"
                " updated_at = unixepoch()"
                " WHERE factory_pr_stage_retry.last_run_id < excluded.last_run_id"
                " RETURNING 1 AS x",
                {"repo": repo, "pr": pr_num, "sha": sha, "run": number},
            )
        )
    if not statements:
        return 0
    return sum(len(rows) for rows in core.store().batch(statements))


def rank_workers(repo: str, workers: list[str]) -> list[str]:
    if not core.enabled():
        return workers
    names = roster()
    marks, params = placeholders("w", sorted(set(workers)))
    rows = core.store().read(
        "SELECT worker, sum(attempts) AS attempts, sum(productive) AS productive"
        " FROM factory_worker_metrics WHERE repo = :repo"
        f" AND worker IN ({marks or 'NULL'}) GROUP BY worker",
        {**params, "repo": repo},
    )
    scores = {str(r["worker"]): (int(r["productive"]), int(r["attempts"])) for r in rows}
    groups: dict[str, list[tuple[int, str]]] = {}
    for index, worker in enumerate(workers):
        groups.setdefault(names.get(worker, ("unknown", ""))[0], []).append((index, worker))
    for entries in groups.values():
        ordered = sorted(
            (w for _, w in entries),
            key=lambda w: (
                -((scores[w][0] / scores[w][1]) if w in scores and scores[w][1] >= 3 else 0.20),
                w,
            ),
        )
        for (position, _), replacement in zip(entries, ordered, strict=True):
            workers[position] = replacement
    return workers


def stats(repo: str) -> dict[str, Any]:
    workers, cooling = core.store().reads(
        [
            (
                "SELECT worker, source, model, attempts, productive, completed, failures"
                " FROM factory_worker_metrics WHERE repo = :repo"
                " ORDER BY productive DESC, attempts DESC LIMIT 100",
                {"repo": repo},
            ),
            (
                "SELECT source, model, status, cooldown_until FROM factory_model_health"
                " INDEXED BY idx_factory_model_health_blocking WHERE repo = :repo AND status IN ('retired', 'unavailable', 'cooling')"
                " AND cooldown_until > unixepoch() ORDER BY cooldown_until",
                {"repo": repo},
            ),
        ]
    )
    for row in cooling:
        row["cooldown_until"] = _when(row["cooldown_until"])
    return {"workers": workers, "cooling": cooling}


def record_runtime(repo: str, run_id: int, outcome: dict[str, Any]) -> bool:
    if not core.enabled():
        raise RuntimeError("trusted runtime requires the factory store")
    from factory_turso_runtime import project_runtime

    return project_runtime(core.store(), repo, run_id, outcome)
