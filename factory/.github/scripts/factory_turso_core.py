"""Shared statements for the libSQL factory store: lease rows, events, guards and reconciliation.

Every write is one ``Store.batch``. A batch has one anchor statement whose
WHERE clause holds every authorizing check; each later statement runs only if
the anchor's row exists with this call's identity (``claim_id`` or a unique
``event_id``). The batch is one write transaction, so nothing interleaves.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import factory_libsql as libsql
from factory_libsql import Statement, Store, StoreAmbiguous, placeholders

REQUIRED_ENV = "FACTORY_STATE_REQUIRED"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HEAD_RE = re.compile(r"^[a-f0-9]{40}$")
_STORE: Store | None = None

LEASE_COLUMNS = (
    "lease_key, repo, kind, number, claim_id, worker, operation, external, wip,"
    " source, slot, acquired_at, expires_at, targets, detail"
)
EVENT_FIELDS = ("event_id", "run_id", "worker", "claim_id", "source", "model", "outcome_class")


USAGE_FILE_ENV = "FACTORY_DB_USAGE_FILE"
METRICS_FILE_ENV = "FACTORY_STATE_METRICS_FILE"


class ClaimsPaused(libsql.StoreError):
    """New factory claims are paused: store usage is critical or the quota is exhausted."""


def claims_paused() -> str:
    """Why the factory may not start new claims now, or ``""``.

    The report is the published ``db_usage`` value the workflow saved in
    ``FACTORY_DB_USAGE_FILE``. A missing or unreadable report never pauses;
    a ``BLOCKED`` store still refuses every request on its own.
    """
    from factory_turso_usage import pause_new_claims

    report: Any = None
    path = os.environ.get(USAGE_FILE_ENV, "")
    if path:
        try:
            report = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = None
    paused, reason = pause_new_claims(report if isinstance(report, Mapping) else None)
    return reason if paused else ""


def refuse_if_paused() -> None:
    reason = claims_paused()
    if reason:
        raise ClaimsPaused(reason)


def enabled() -> bool:
    return libsql.configured()


def required() -> bool:
    return os.environ.get(REQUIRED_ENV) == "true"


def store() -> Store:
    """One store per process; the schema is created on first use."""
    global _STORE
    if _STORE is None:
        candidate = libsql.connect()
        libsql.ensure_schema(candidate)
        _STORE = candidate
        if os.environ.get(METRICS_FILE_ENV):
            import atexit

            atexit.register(_record_usage, candidate)
    return _STORE


def _record_usage(db: Store) -> None:
    """Append this process's request and row counts; never SQL, keys or values."""
    try:
        with Path(os.environ[METRICS_FILE_ENV]).open("a", encoding="utf-8") as output:
            output.write(json.dumps(dict(db.usage), sort_keys=True) + "\n")
    except (KeyError, OSError):
        pass


def use_store(candidate: Store | None) -> None:
    """Point this process at a specific store (tests, export, self-test)."""
    global _STORE
    _STORE = candidate


def free_keys_guard(keys: Sequence[str], prefix: str = "fk") -> tuple[str, dict[str, Any]]:
    """True when no key is held by an active claim or by any external claim."""
    marks, params = placeholders(prefix, keys)
    return (
        f"NOT EXISTS (SELECT 1 FROM factory_lease WHERE lease_key IN ({marks})"
        " AND (expires_at > unixepoch() OR external = 1))",
        params,
    )


def held_guard(key: str, token: str) -> tuple[str, dict[str, Any]]:
    return (
        "EXISTS (SELECT 1 FROM factory_lease WHERE lease_key = :g_key AND claim_id = :g_token)",
        {"g_key": key, "g_token": token},
    )


def event_guard(event_id: str) -> tuple[str, dict[str, Any]]:
    return (
        "EXISTS (SELECT 1 FROM factory_state_events WHERE event_id = :g_event)",
        {"g_event": event_id},
    )


def lease_upsert(
    *,
    key: str,
    repo: str,
    kind: str,
    token: str,
    ttl: int,
    guard: tuple[str, Mapping[str, Any]],
    number: int | None = None,
    worker: str | None = None,
    operation: str | None = None,
    external: bool = False,
    wip: bool = False,
    source: str | None = None,
    slot: str | None = None,
    targets: Sequence[int] = (),
    detail: Mapping[str, Any] | None = None,
) -> Statement:
    """Write one lease row when ``guard`` holds; it only ever replaces an expired factory row."""
    condition, guard_params = guard
    sql = (
        f"INSERT INTO factory_lease ({LEASE_COLUMNS})"
        " SELECT :key, :repo, :kind, :number, :token, :worker, :operation, :external, :wip,"
        " :source, :slot, unixepoch(), unixepoch() + :ttl, :targets, :detail"
        f" WHERE {condition}"
        " ON CONFLICT(lease_key) DO UPDATE SET repo = excluded.repo, kind = excluded.kind,"
        " number = excluded.number, claim_id = excluded.claim_id, worker = excluded.worker,"
        " operation = excluded.operation, external = excluded.external, wip = excluded.wip,"
        " source = excluded.source, slot = excluded.slot, acquired_at = excluded.acquired_at,"
        " expires_at = excluded.expires_at, targets = excluded.targets, detail = excluded.detail"
        " WHERE factory_lease.expires_at <= unixepoch() AND factory_lease.external = 0"
        " RETURNING claim_id, expires_at"
    )
    params = {
        **guard_params,
        "key": key,
        "repo": repo,
        "kind": kind,
        "number": number,
        "token": token,
        "worker": worker,
        "operation": operation,
        "external": int(external),
        "wip": int(wip),
        "source": source,
        "slot": slot,
        "ttl": int(ttl),
        "targets": json.dumps(sorted(set(targets))),
        "detail": json.dumps(dict(detail or {}), sort_keys=True),
    }
    return sql, params


def event_insert(
    repo: str,
    origin: str,
    event_type: str,
    detail: Mapping[str, Any],
    *,
    subject_type: str | None,
    subject_number: int | None,
    guard: tuple[str, Mapping[str, Any]] | None = None,
) -> Statement:
    """Append one event; a repeated ``event_id`` is ignored, so ingestion is idempotent."""
    condition, guard_params = guard or ("1", {})
    params: dict[str, Any] = {
        **guard_params,
        "e_repo": repo,
        "e_origin": origin,
        "e_type": event_type,
        "e_subject": subject_type,
        "e_number": subject_number,
        "e_detail": json.dumps({**detail, "repo": repo}, sort_keys=True, default=str),
    }
    for field in EVENT_FIELDS:
        value = detail.get(field)
        params[f"e_{field}"] = None if value is None or value == "" else str(value)
    sql = (
        "INSERT OR IGNORE INTO factory_state_events (repo, origin, event_type, subject_type,"
        " subject_number, event_id, run_id, worker, claim_id, source, model, outcome_class,"
        " created_at, detail)"
        " SELECT :e_repo, :e_origin, :e_type, :e_subject, :e_number, :e_event_id, :e_run_id,"
        " :e_worker, :e_claim_id, :e_source, :e_model, :e_outcome_class, unixepoch(), :e_detail"
        f" WHERE {condition} RETURNING id"
    )
    return sql, params


def reconciled_batch(
    db: Store, statements: Sequence[Statement], committed: Callable[[], bool]
) -> list[list[dict[str, Any]]] | None:
    """Send one write batch; after a lost response, ask ``committed`` before one resend.

    Returns the batch result, or ``None`` when the lost request is proven to
    have committed (the caller then reads its own claim back). A resend is safe
    because every batch is fenced by an identity this call generated.
    """
    try:
        return db.batch(statements)
    except StoreAmbiguous:
        if committed():
            return None
        return db.batch(statements)


def claim_of(db: Store, key: str) -> str | None:
    rows = db.read("SELECT claim_id FROM factory_lease WHERE lease_key = :k", {"k": key})
    return str(rows[0]["claim_id"]) if rows else None


def event_exists(db: Store, event_id: str) -> bool:
    rows = db.read("SELECT 1 AS x FROM factory_state_events WHERE event_id = :e", {"e": event_id})
    return bool(rows)


def lease_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The operator view of one lease row, the same shape the retired Neon ledger returned."""
    from datetime import UTC, datetime

    targets = json.loads(row.get("targets") or "[]")
    detail = json.loads(row.get("detail") or "{}")
    acquired = row.get("acquired_at")
    acquired_iso = (
        "" if acquired is None else datetime.fromtimestamp(int(acquired), UTC).isoformat()
    )
    return {
        "key": row["lease_key"],
        "claim_id": str(row.get("claim_id") or ""),
        "worker": str(row.get("worker") or ""),
        "operation": str(row.get("operation") or ""),
        "wip": bool(row.get("wip")),
        "targets": [int(n) for n in targets if str(n).isdecimal()],
        "expires_at": datetime.fromtimestamp(int(row["expires_at"]), UTC).isoformat(),
        "active": bool(row.get("active")),
        "acquired_at": acquired_iso,
        "run_id": str(detail.get("run_id") or "") if isinstance(detail, dict) else "",
    }
