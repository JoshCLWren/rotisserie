"""A fake Neon source and one sample of every Neon row family for the migration tests.

Rows carry the raw types asyncpg returns: aware datetimes, ``date``, ``UUID``
and jsonb as text. The kv ``payload`` is ``None`` for families the reader does
not fetch, as the real reader's ``CASE`` leaves it.
"""

from __future__ import annotations

import importlib
import json
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

KEYSETS = importlib.import_module("factory_turso_migrate_neon").KEYSETS
REPO = "acme/widgets"
CLAIM = "11111111-1111-4111-8111-111111111111"
OPERATION = "22222222-2222-4222-8222-222222222222"
SLOT = "33333333-3333-4333-8333-333333333333"
EXTERNAL = "44444444-4444-4444-8444-444444444444"
MODEL = "org/model:free"


def at(offset: int) -> datetime:
    return datetime.fromtimestamp(int(time.time()) + offset, UTC)


def workers() -> dict[str, list[str]]:
    slots = importlib.import_module("factory_worker_slots").slots
    found: dict[str, list[str]] = {}
    for entry in slots():
        found.setdefault(entry.source, []).append(entry.worker)
    return found


class FakeNeon:
    def __init__(self, data: dict[str, list[dict[str, Any]]], now: int | None = None) -> None:
        self.data = data
        self._now = int(time.time()) if now is None else now
        self.pages = 0

    def tables(self) -> set[str]:
        return set(self.data)

    def now(self) -> int:
        return self._now

    def count(self, table: str) -> int:
        return len(self.data[table])

    def page(self, table: str, after: Sequence[Any] | None, limit: int) -> list[dict[str, Any]]:
        self.pages += 1
        keys = KEYSETS[table]
        rows = sorted(self.data[table], key=lambda row: tuple(row[k] for k in keys))
        if after is not None:
            rows = [row for row in rows if tuple(row[k] for k in keys) > tuple(after)]
        return [dict(row) for row in rows[:limit]]


def kv(key: str, payload: Any, *, expires: int = 600, fetched: int = -60) -> dict[str, Any]:
    return {
        "cache_key": key,
        "payload": None if payload is None else json.dumps(payload),
        "fetched_at": at(fetched),
        "expires_at": at(expires),
        "updated_at": at(fetched),
    }


def sample(worker: str, slot_worker: str) -> dict[str, list[dict[str, Any]]]:
    lease = f"factory-lease-v1:{REPO}"
    claim = {"claim_id": CLAIM, "worker": worker, "targets": [5]}
    slot = {
        "claim_id": SLOT,
        "worker": slot_worker,
        "targets": [12],
        "operation": "slot:attempt-1",
        "slot": "attempt-1",
        "source": "nvidia",
    }
    outside = {
        "claim_id": EXTERNAL,
        "operation": "external:octo/codex",
        "external": True,
        "actor": "octo",
        "client": "codex",
        "targets": [20],
        "target": "issue:20",
        "action": "implement",
        "head": None,
        "wip": True,
        "offer_at": at(-900).isoformat(),
    }
    fib = {
        "source": "nvidia",
        "outcome": "work_failure",
        "streak": 2,
        "run_id": 50,
        "suspended": False,
        "next_retry_at": at(3600).isoformat(),
    }
    capacity = {
        "outcome": "provider_quota",
        "kind": "daily",
        "scope": "account",
        "reset_source": "policy",
        "run_id": 51,
    }
    kv_rows = [
        kv(f"{lease}:worker:{worker}", [claim]),
        kv(f"{lease}:target:5", [claim]),
        kv(f"{lease}:target:9", [{"claim_id": OPERATION, "operation": "merge", "targets": [9]}]),
        kv(f"{lease}:worker:{slot_worker}", [slot]),
        kv(f"{lease}:slot:12:attempt-1", [slot]),
        kv(f"{lease}:external:{EXTERNAL}", [outside], expires=-100),
        kv(f"{lease}:target:20", [outside], expires=-100),
        kv(f"{lease}:worker:999", [{"worker": "999"}]),
        kv(
            f"factory-worker-backoff-v1:{REPO}:worker:{worker}",
            [{**fib, "model": "m1"}],
            expires=90 * 86400,
            fetched=-10,
        ),
        kv(
            f"factory-model-backoff-v1:{REPO}:source:nvidia:model:{MODEL}",
            [{**fib, "model": MODEL, "scope": "model"}],
            expires=90 * 86400,
            fetched=-10,
        ),
        kv(
            f"factory-provider-backoff-v1:{REPO}:source:openrouter",
            [{**capacity, "source": "openrouter", "model": "x", "next_retry_at": "soon"}],
        ),
        kv(
            f"factory-provider-backoff-v1:{REPO}:source:groq",
            [{**capacity, "source": "groq", "model": "y", "next_retry_at": at(600).isoformat()}],
            expires=600,
        ),
        kv(
            f"factory-credit-pool-v1:{REPO}:source:kilo",
            [{**capacity, "source": "kilo", "model": "z", "scope": "pool"}],
            expires=900,
        ),
        kv(f"factory-model-health-reset-v1:{REPO}", [], fetched=-1000),
        kv(f"factory-projection-v1:{REPO}:pr:1", None),
        kv(f"factory-projection-v1:{REPO}:issue:2", None),
        kv(f"factory-github-get-v1:{REPO}:abc", None),
        kv(f"{REPO}:open-issues", None),
        kv("something-private:token", None),
    ]
    events = [
        {
            "id": 1,
            "source": "github-actions",
            "event_type": "worker_assigned",
            "subject_type": "worker",
            "subject_number": int(worker),
            "created_at": at(-5),
            "detail": json.dumps(
                {
                    "claim_id": CLAIM,
                    "worker": worker,
                    "targets": [5],
                    "run_id": "100",
                    "source": "nvidia",
                    "repo": REPO,
                }
            ),
        },
        {
            "id": 2,
            "source": "github-actions",
            "event_type": "model_attempt",
            "subject_type": "worker",
            "subject_number": int(worker),
            "created_at": at(-4),
            "detail": json.dumps(
                {"event_id": "e2", "source": "nvidia", "model": "m1", "outcome_class": "success"}
            ),
        },
        {
            "id": 3,
            "source": "rotisserie-external",
            "event_type": "external_reserved",
            "subject_type": "target",
            "subject_number": 20,
            "created_at": at(-3),
            "detail": json.dumps({"event_id": "e3", "repository": REPO, "run_id": ""}),
        },
        {
            "id": 4,
            "source": "github-actions",
            "event_type": "model_attempt",
            "subject_type": None,
            "subject_number": None,
            "created_at": at(-2),
            "detail": json.dumps({"event_id": "e2"}),
        },
        {
            "id": 5,
            "source": "github-actions",
            "event_type": "cache_refresh",
            "subject_type": None,
            "subject_number": None,
            "created_at": at(-1),
            "detail": "[1]",
        },
    ]
    stamp = at(-30)
    return {
        "factory_state_kv": kv_rows,
        "factory_state_events": events,
        "factory_model_health": [
            {
                "repo": REPO,
                "source": "nvidia",
                "model": "cool-model",
                "status": "cooling",
                "last_outcome": "provider_throttle",
                "cooldown_until": at(1800),
                "last_verified_at": None,
                "last_run_id": 40,
                "updated_at": stamp,
            },
            {
                "repo": REPO,
                "source": "nvidia",
                "model": "ok-model",
                "status": "healthy",
                "last_outcome": "success",
                "cooldown_until": None,
                "last_verified_at": stamp,
                "last_run_id": 41,
                "updated_at": stamp,
            },
            {
                "repo": REPO,
                "source": "nvidia",
                "model": "odd",
                "status": "sleeping",
                "last_outcome": None,
                "cooldown_until": None,
                "last_verified_at": None,
                "last_run_id": 0,
                "updated_at": stamp,
            },
        ],
        "factory_worker_metrics": [
            {
                "repo": REPO,
                "worker": worker,
                "source": "nvidia",
                "model": "m1",
                "attempts": 4,
                "productive": 2,
                "completed": 2,
                "failures": 1,
                "last_run_id": 50,
                "updated_at": stamp,
            }
        ],
        "factory_provider_budget": [
            _budget("nvidia", concurrency_limit=2),
            _budget("groq", cooldown_until=at(60)),
            _budget("kilo", daily_limit=0, quota_reset_at=at(60)),
            _budget("zai"),
        ],
        "factory_provider_reservation": [
            _reservation(uuid.UUID(CLAIM), worker, 600),
            _reservation(uuid.uuid4(), worker, -600),
        ],
        "factory_pr_stage_retry": [
            {
                "repo": REPO,
                "pr_number": 7,
                "head_sha": "a" * 40,
                "stage": "factory:review",
                "attempts": 2,
                "outcome": "work_failure",
                "retry_after": at(300),
                "last_run_id": 52,
                "updated_at": stamp,
            }
        ],
        "factory_runtime_event_keys": [{"repo": REPO, "run_id": 50}, {"repo": REPO, "run_id": 51}],
        "factory_model_attempt_daily": [
            {
                "repo": REPO,
                "day": date.today() - timedelta(days=200),
                "source": "nvidia",
                "model": "m1",
                "outcome_class": "success",
                "attempts": 9,
                "timed_attempts": 8,
                "duration_ms": 1200,
            }
        ],
        "factory_reconciliation_cursor": [],
        "factory_check_projection": [{"repo": REPO, "pr": n} for n in range(3)],
    }


def _budget(source: str, **values: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "repo": REPO,
        "source": source,
        "cooldown_until": None,
        "quota_reset_at": None,
        "concurrency_limit": None,
        "daily_limit": None,
        "usage_day": date.today(),
        "daily_used": 3,
        "last_run_id": 0,
        "last_outcome": None,
        "updated_at": at(-30),
    }
    return {**row, **values}


def _reservation(claim: uuid.UUID, worker: str, expires: int) -> dict[str, Any]:
    return {
        "claim_id": claim,
        "repo": REPO,
        "worker": worker,
        "source": "nvidia",
        "expires_at": at(expires),
    }


def events(count: int, start: int = 1) -> list[dict[str, Any]]:
    """Synthetic model attempts; every third one has no ``event_id``."""
    rows = []
    for number in range(start, start + count):
        detail: dict[str, Any] = {"repo": REPO, "source": "nvidia", "model": "m", "run_id": "9"}
        if number % 3:
            detail["event_id"] = f"attempt:{number}"
        rows.append(
            {
                "id": number,
                "source": "github-actions",
                "event_type": "model_attempt",
                "subject_type": "worker",
                "subject_number": 1,
                "created_at": at(-number),
                "detail": json.dumps(detail),
            }
        )
    return rows
