"""The row mapping is total: a row the store would refuse becomes a reason, never an error."""

from __future__ import annotations

import importlib
import uuid
from datetime import date, datetime
from typing import Any

import pytest

from tests import turso_migrate_support as support

mapping = importlib.import_module("factory_turso_migrate_map")
rows = importlib.import_module("factory_turso_migrate_rows")
REPO = support.REPO
LEASE = f"factory-lease-v1:{REPO}"


@pytest.mark.parametrize(
    ("key", "payload", "reason"),
    [
        (f"{LEASE}:worker:abc", [{"claim_id": "c"}], "key_invalid"),
        (f"{LEASE}:target:x", [{"claim_id": "c"}], "key_invalid"),
        (f"{LEASE}:mystery:1", [{"claim_id": "c"}], "kind_invalid"),
        (f"{LEASE}:worker:1", "not json", "json_invalid"),
        (f"{LEASE}:worker:1", [], "payload_invalid"),
        (f"{LEASE}:worker:1", [{"claim_id": "c", "targets": ["x"]}], "targets_invalid"),
        ("factory-lease-v1:bad repo:worker:1", [{"claim_id": "c"}], "repo_invalid"),
        (f"factory-worker-backoff-v1:{REPO}:worker:x", [{}], "key_invalid"),
        (f"factory-model-backoff-v1:{REPO}:source:s", [{}], "key_invalid"),
        (f"factory-worker-backoff-v1:{REPO}:worker:1", [{"streak": "many"}], "integer_invalid"),
        (f"factory-worker-backoff-v1:{REPO}:worker:1", [{"streak": True}], "integer_invalid"),
    ],
)
def test_bad_kv_rows_become_reasons(key: str, payload: Any, reason: str) -> None:
    row = support.kv(key, payload)
    if payload == "not json":
        row["payload"] = "{"
    found = mapping.map_kv(row)
    assert (found.values, found.reason) == (None, reason)


def test_a_lease_without_an_expiry_is_refused() -> None:
    row = support.kv(f"{LEASE}:worker:1", [{"claim_id": "c"}])
    row["expires_at"] = None
    assert mapping.map_kv(row).reason == "expires_at_missing"


def test_a_model_name_with_colons_comes_from_the_payload_or_the_key() -> None:
    key = f"factory-model-backoff-v1:{REPO}:source:nvidia:model:a:b:c"
    found = mapping.map_kv(support.kv(key, [{"outcome": "work_failure"}]))
    assert found.values and (found.values["source"], found.values["model"]) == ("nvidia", "a:b:c")
    assert found.values["worker"] == "" and found.values["kind"] == "model"


def test_event_fields_follow_event_insert() -> None:
    row = {
        "id": 7,
        "source": "github-actions",
        "event_type": "worker_failed",
        "subject_type": "worker",
        "subject_number": 3,
        "created_at": datetime.fromisoformat("2026-01-02T03:04:05+00:00"),
        "detail": '{"run_id": 12, "worker": "", "model": "m", "z": 1, "repo": "acme/widgets"}',
    }
    found = rows.map_event(row, "other/repo")
    assert found.values is not None and found.reason == ""
    assert found.values["run_id"] == "12" and found.values["worker"] is None
    assert found.values["event_id"] == "neon:7" and found.values["created_at"] == 1767323045
    assert found.values["detail"] == (
        '{"model": "m", "repo": "acme/widgets", "run_id": 12, "worker": "", "z": 1}'
    )


def test_an_event_without_a_repo_takes_the_migration_repo() -> None:
    row = {"id": 8, "source": "x", "event_type": "y", "created_at": 5, "detail": {}}
    found = rows.map_event(row, REPO)
    assert found.reason == "repo_defaulted" and found.values and found.values["repo"] == REPO


@pytest.mark.parametrize(
    ("table", "row", "reason"),
    [
        ("factory_model_health", {"repo": REPO}, "row_invalid"),
        (
            "factory_worker_metrics",
            {"repo": REPO, "worker": 1, "source": "s", "model": "m"},
            "worker_invalid",
        ),
        (
            "factory_provider_budget",
            {"repo": REPO, "source": "s", "concurrency_limit": 0},
            "limit_invalid",
        ),
        ("factory_pr_stage_retry", {"repo": REPO, "pr_number": "x"}, "integer_invalid"),
        ("factory_runtime_event_keys", {"repo": REPO, "run_id": None}, "run_id_missing"),
        ("factory_model_attempt_daily", {"repo": REPO, "day": date.today()}, "row_invalid"),
    ],
)
def test_bad_table_rows_become_reasons(table: str, row: dict[str, Any], reason: str) -> None:
    found = rows.map_table(table, {**row, "updated_at": 1}, 100)
    assert (found.values, found.reason) == (None, reason)


def test_a_live_reservation_keeps_its_uuid_as_text() -> None:
    claim = uuid.uuid4()
    row = {"claim_id": claim, "repo": REPO, "worker": "1", "source": "s", "expires_at": 200}
    found = rows.map_table("factory_provider_reservation", row, 100)
    assert found.values and found.values["claim_id"] == str(claim)


def test_every_insert_names_only_destination_columns() -> None:
    sql, params = mapping.insert_statement("factory_repo_state", {"repo": REPO, "x": 1})
    assert "ON CONFLICT DO NOTHING" in sql and set(params) == {"repo", "health_reset_at"}
