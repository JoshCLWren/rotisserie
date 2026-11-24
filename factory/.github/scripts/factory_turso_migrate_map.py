"""Pure mapping from Neon factory rows to libSQL store rows, standard library only.

Each function takes one Neon row, as asyncpg returns it (datetime, date, UUID,
jsonb as text), and returns a ``Mapped``: the destination table and its typed
values, or a skip reason. The values are the ones the ``factory_turso_*``
writers would have stored, so their read functions read a migrated row the
same way as a native one. Every destination constraint is checked here, so a
row the store would refuse becomes a counted reason, never a failed batch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

LEASE_PREFIX = "factory-lease-v1"
WORKER_BACKOFF = "factory-worker-backoff-v1"
MODEL_BACKOFF = "factory-model-backoff-v1"
PROVIDER_BACKOFF = "factory-provider-backoff-v1"
POOL_BACKOFF = "factory-credit-pool-v1"
HEALTH_RESET = "factory-model-health-reset-v1"
PROJECTION = "factory-projection-v1"
BACKOFF_KINDS = {
    WORKER_BACKOFF: "worker",
    MODEL_BACKOFF: "model",
    PROVIDER_BACKOFF: "provider",
    POOL_BACKOFF: "pool",
}
NOT_MIGRATED = (
    "factory-github-get-v1",
    PROJECTION,
    "factory-fleet-notes-v1",
    "factory-incident-superseded-v1",
    "factory-state-upgrades-v1",
)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKER_RE = re.compile(r"^[0-9]+$")
HEALTH_STATUSES = {"unknown", "healthy", "cooling", "unavailable", "retired"}
TYPED_BACKOFF = {"source", "model", "outcome", "streak", "run_id", "next_retry_at"}
EXTERNAL_DETAIL = ("actor", "client", "target", "action", "head", "offer_at")
EVENT_FIELDS = ("event_id", "run_id", "worker", "claim_id", "source", "model", "outcome_class")
INT, TEXT = "int", "text"

# Destination columns: type and whether NULL is allowed (the STRICT schema).
COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "factory_lease": {
        "lease_key": (TEXT, False),
        "repo": (TEXT, False),
        "kind": (TEXT, False),
        "number": (INT, True),
        "claim_id": (TEXT, False),
        "worker": (TEXT, True),
        "operation": (TEXT, True),
        "external": (INT, False),
        "wip": (INT, False),
        "source": (TEXT, True),
        "slot": (TEXT, True),
        "acquired_at": (INT, False),
        "expires_at": (INT, False),
        "targets": (TEXT, False),
        "detail": (TEXT, False),
    },
    "factory_backoff": {
        "backoff_key": (TEXT, False),
        "repo": (TEXT, False),
        "kind": (TEXT, False),
        "source": (TEXT, False),
        "model": (TEXT, False),
        "worker": (TEXT, False),
        "outcome": (TEXT, False),
        "streak": (INT, False),
        "run_id": (INT, False),
        "next_retry_at": (INT, True),
        "expires_at": (INT, False),
        "updated_at": (INT, False),
        "detail": (TEXT, False),
    },
    "factory_repo_state": {"repo": (TEXT, False), "health_reset_at": (INT, True)},
    "factory_state_events": {
        "repo": (TEXT, False),
        "origin": (TEXT, False),
        "event_type": (TEXT, False),
        "subject_type": (TEXT, True),
        "subject_number": (INT, True),
        "event_id": (TEXT, False),
        "run_id": (TEXT, True),
        "worker": (TEXT, True),
        "claim_id": (TEXT, True),
        "source": (TEXT, True),
        "model": (TEXT, True),
        "outcome_class": (TEXT, True),
        "created_at": (INT, False),
        "detail": (TEXT, False),
    },
    "factory_model_health": {
        "repo": (TEXT, False),
        "source": (TEXT, False),
        "model": (TEXT, False),
        "status": (TEXT, False),
        "last_outcome": (TEXT, True),
        "cooldown_until": (INT, True),
        "last_verified_at": (INT, True),
        "last_run_id": (INT, False),
        "updated_at": (INT, False),
    },
    "factory_worker_metrics": {
        "repo": (TEXT, False),
        "worker": (TEXT, False),
        "source": (TEXT, False),
        "model": (TEXT, False),
        "attempts": (INT, False),
        "productive": (INT, False),
        "completed": (INT, False),
        "failures": (INT, False),
        "last_run_id": (INT, False),
        "updated_at": (INT, False),
    },
    "factory_provider_budget": {
        "repo": (TEXT, False),
        "source": (TEXT, False),
        "concurrency_limit": (INT, True),
        "daily_limit": (INT, True),
        "updated_at": (INT, False),
    },
    "factory_provider_reservation": {
        "claim_id": (TEXT, False),
        "repo": (TEXT, False),
        "worker": (TEXT, False),
        "source": (TEXT, False),
        "expires_at": (INT, False),
    },
    "factory_pr_stage_retry": {
        "repo": (TEXT, False),
        "pr_number": (INT, False),
        "head_sha": (TEXT, False),
        "stage": (TEXT, False),
        "attempts": (INT, False),
        "outcome": (TEXT, False),
        "retry_after": (INT, True),
        "last_run_id": (INT, False),
        "updated_at": (INT, False),
    },
    "factory_runtime_event_keys": {"repo": (TEXT, False), "run_id": (INT, False)},
    "factory_model_attempt_daily": {
        "repo": (TEXT, False),
        "day": (TEXT, False),
        "source": (TEXT, False),
        "model": (TEXT, False),
        "outcome_class": (TEXT, False),
        "attempts": (INT, False),
        "timed_attempts": (INT, False),
        "duration_ms": (INT, False),
    },
}
# The destination identity of each row: what a re-run and the count check look up.
KEYS: dict[str, tuple[str, ...]] = {
    "factory_lease": ("lease_key",),
    "factory_backoff": ("backoff_key",),
    "factory_repo_state": ("repo",),
    "factory_state_events": ("event_id",),
    "factory_model_health": ("repo", "source", "model"),
    "factory_worker_metrics": ("repo", "worker", "source", "model"),
    "factory_provider_budget": ("repo", "source"),
    "factory_provider_reservation": ("claim_id",),
    "factory_pr_stage_retry": ("repo", "pr_number", "head_sha", "stage"),
    "factory_runtime_event_keys": ("repo", "run_id"),
    "factory_model_attempt_daily": ("repo", "day", "source", "model", "outcome_class"),
}


@dataclass(frozen=True)
class Mapped:
    group: str
    table: str = ""
    values: dict[str, Any] | None = None
    reason: str = ""

    def key(self) -> tuple[Any, ...]:
        assert self.values is not None
        return tuple(self.values[column] for column in KEYS[self.table])


class Invalid(ValueError):
    """The row cannot be stored; the message is a fixed reason category."""


def epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise Invalid("timestamp_invalid")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise Invalid("timestamp_invalid") from None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        try:
            return int(value.timestamp())
        except OverflowError, OSError, ValueError:
            raise Invalid("timestamp_invalid") from None
    raise Invalid("timestamp_invalid")


def _json(value: Any) -> Any:
    if isinstance(value, str | bytes):
        try:
            return json.loads(value)
        except ValueError:
            raise Invalid("json_invalid") from None
    return value


def _first(payload: Any) -> dict[str, Any]:
    found = _json(payload)
    if isinstance(found, list) and found and isinstance(found[0], dict):
        return found[0]
    raise Invalid("payload_invalid")


def _int(value: Any, default: int | None = 0) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise Invalid("integer_invalid")
    try:
        return int(value)
    except TypeError, ValueError:
        raise Invalid("integer_invalid") from None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date | datetime):
        return value.isoformat()
    return str(value)


def check(table: str, values: dict[str, Any]) -> None:
    """Raise ``Invalid`` unless the STRICT table would accept every value."""
    for column, (kind, nullable) in COLUMNS[table].items():
        value = values.get(column)
        if value is None:
            if not nullable:
                raise Invalid(f"{column}_missing")
            continue
        wanted = str if kind == TEXT else int
        if isinstance(value, bool) or not isinstance(value, wanted):
            raise Invalid(f"{column}_invalid")
    if "repo" in values and not REPO_RE.fullmatch(str(values["repo"])):
        raise Invalid("repo_invalid")


def _done(group: str, table: str, values: dict[str, Any]) -> Mapped:
    check(table, values)
    return Mapped(group, table, values)


def _family(key: str) -> str:
    head = key.split(":", 1)[0]
    if head in NOT_MIGRATED or head in BACKOFF_KINDS or head in (LEASE_PREFIX, HEALTH_RESET):
        return head
    if key.endswith((":open-issues", ":open-prs")):
        return "open-lists"
    return "other"


def map_kv(row: dict[str, Any]) -> Mapped:
    key = str(row["cache_key"])
    family = _family(key)
    group = f"kv:{family}"
    try:
        if family == LEASE_PREFIX:
            return _lease(group, key, row)
        if family in BACKOFF_KINDS:
            return _backoff(group, family, key, row)
        if family == HEALTH_RESET:
            repo = key.removeprefix(f"{HEALTH_RESET}:")
            values = {"repo": repo, "health_reset_at": epoch(row.get("updated_at"))}
            return _done(group, "factory_repo_state", values)
    except Invalid as error:
        return Mapped(group, reason=str(error))
    return Mapped("kv:not_migrated", reason=family)


def _lease(group: str, key: str, row: dict[str, Any]) -> Mapped:
    parts = key.removeprefix(f"{LEASE_PREFIX}:").split(":")
    if len(parts) < 3:
        raise Invalid("key_invalid")
    repo, kind = parts[0], parts[1]
    first = _first(row["payload"])
    number: int | None = None
    slot = first.get("slot")
    if kind == "worker":
        if len(parts) != 3 or not WORKER_RE.fullmatch(parts[2]):
            raise Invalid("key_invalid")
    elif kind in ("target", "slot"):
        if not parts[2].isdecimal() or (kind == "target" and len(parts) != 3):
            raise Invalid("key_invalid")
        number = int(parts[2])
        if kind == "slot":
            slot = ":".join(parts[3:]) or slot
    elif kind == "external":
        if len(parts) != 3:
            raise Invalid("key_invalid")
    else:
        raise Invalid("kind_invalid")
    claim = str(first.get("claim_id") or "")
    if not claim:
        raise Invalid("claim_id_missing")
    external = kind == "external" or first.get("external") is True
    targets = first.get("targets") or []
    if not isinstance(targets, list) or not all(str(n).isdecimal() for n in targets):
        raise Invalid("targets_invalid")
    detail = {name: first.get(name) for name in EXTERNAL_DETAIL} if external else {}
    worker = first.get("worker")
    values = {
        "lease_key": key,
        "repo": repo,
        "kind": kind,
        "number": number,
        "claim_id": claim,
        "worker": None if worker in (None, "") else str(worker),
        "operation": _text(first.get("operation") or None),
        "external": int(external),
        "wip": int(first.get("wip") is True),
        "source": _text(first.get("source") or None),
        "slot": _text(slot or None),
        "acquired_at": epoch(row.get("fetched_at")) or epoch(row.get("updated_at")),
        "expires_at": epoch(row.get("expires_at")),
        "targets": json.dumps(sorted({int(n) for n in targets})),
        "detail": json.dumps(detail, sort_keys=True),
    }
    return _done(group, "factory_lease", values)


def _backoff(group: str, family: str, key: str, row: dict[str, Any]) -> Mapped:
    rest = key.removeprefix(f"{family}:")
    repo, _, scope = rest.partition(":")
    kind = BACKOFF_KINDS[family]
    worker = ""
    key_source, key_model = "", ""
    if kind == "worker":
        found = re.fullmatch(r"worker:([0-9]+)", scope)
        if not found:
            raise Invalid("key_invalid")
        worker = found.group(1)
    elif kind == "model":
        found = re.fullmatch(r"source:([^:]+):model:(.+)", scope, re.DOTALL)
        if not found:
            raise Invalid("key_invalid")
        key_source, key_model = found.group(1), found.group(2)
    else:
        found = re.fullmatch(r"source:([^:]+)", scope)
        if not found:
            raise Invalid("key_invalid")
        key_source = found.group(1)
    state = _first(row["payload"])
    retry = state.get("next_retry_at")
    values = {
        "backoff_key": key,
        "repo": repo,
        "kind": kind,
        "source": str(state.get("source") or key_source),
        "model": str(state.get("model") or key_model),
        "worker": worker,
        "outcome": str(state.get("outcome") or ""),
        "streak": _int(state.get("streak")),
        "run_id": _int(state.get("run_id")),
        "next_retry_at": None if retry in (None, "") else epoch(retry),
        "expires_at": epoch(row.get("expires_at")),
        "updated_at": epoch(row.get("updated_at")),
        "detail": json.dumps(
            {k: v for k, v in state.items() if k not in TYPED_BACKOFF}, sort_keys=True, default=str
        ),
    }
    return _done(group, "factory_backoff", values)


def insert_statement(table: str, values: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Insert one row unless its key exists; a re-run never overwrites a stored row."""
    columns = list(COLUMNS[table])
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)})"
        f" VALUES ({', '.join(':' + c for c in columns)})"
        " ON CONFLICT DO NOTHING RETURNING 1 AS x"
    )
    return sql, {column: values.get(column) for column in columns}
