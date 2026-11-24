"""Migrated rows read through the libSQL store's own functions exactly as native rows do."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests import turso_migrate_support as support

libsql = importlib.import_module("factory_libsql")
core = importlib.import_module("factory_turso_core")
migrate = importlib.import_module("factory_turso_migrate")
mapping = importlib.import_module("factory_turso_migrate_map")
leases = importlib.import_module("factory_turso_leases")
external = importlib.import_module("factory_turso_external")
exports = importlib.import_module("factory_turso_exports")
operations = importlib.import_module("factory_turso_operations")
backoff = importlib.import_module("factory_turso_backoff")
runtime = importlib.import_module("factory_turso_runtime")
REPO = support.REPO


@pytest.fixture
def migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    nvidia = support.workers()["nvidia"]
    data = support.sample(nvidia[0], nvidia[1])
    db = libsql.FileStore(tmp_path / "state.sqlite")
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(tmp_path / "state.sqlite"))
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    summary = migrate.migrate(support.FakeNeon(data), db, repo=REPO, dry_run=False)
    assert summary["status"] == "ok", summary["mismatches"]
    core.use_store(db)
    try:
        yield db, data, nvidia
    finally:
        core.use_store(None)


def test_leases_list_with_their_claims_and_activity(migrated: Any) -> None:
    _, _, nvidia = migrated
    found = {row["key"]: row for row in leases.list_leases(REPO)}
    prefix = f"factory-lease-v1:{REPO}"
    assert set(found) == {
        f"{prefix}:worker:{nvidia[0]}",
        f"{prefix}:target:5",
        f"{prefix}:target:9",
        f"{prefix}:worker:{nvidia[1]}",
        f"{prefix}:slot:12:attempt-1",
        f"{prefix}:external:{support.EXTERNAL}",
        f"{prefix}:target:20",
    }
    worker = found[f"{prefix}:worker:{nvidia[0]}"]
    assert (worker["claim_id"], worker["targets"], worker["active"]) == (support.CLAIM, [5], True)
    assert found[f"{prefix}:target:9"]["operation"] == "merge"
    assert found[f"{prefix}:target:20"]["active"] is False
    assert leases.worker_claim(REPO, nvidia[0]) == (support.CLAIM, [5])
    assert leases.target_lease(REPO, 5)["claim_id"] == support.CLAIM


def test_an_expired_external_claim_still_holds_its_target(migrated: Any) -> None:
    db, _, _ = migrated
    holder = external.external_holder(REPO, 20)
    assert holder is not None
    assert (holder["claim_id"], holder["actor"], holder["client"]) == (
        support.EXTERNAL,
        "octo",
        "codex",
    )
    assert holder["target"] == "issue:20" and holder["action"] == "implement"
    assert external.external_wip_count(REPO) == 1
    assert leases.release_expired_leases(REPO) == 0
    kinds = db.read(
        "SELECT kind, external FROM factory_lease WHERE claim_id = :c ORDER BY kind",
        {"c": support.EXTERNAL},
    )
    assert kinds == [{"kind": "external", "external": 1}, {"kind": "target", "external": 1}]
    revoked = external.revoke_external(REPO, "operator", support.EXTERNAL, reason="test")
    assert revoked["status"] == "revoked" and revoked["targets"] == [20]
    assert external.external_holder(REPO, 20) is None


def test_slot_rows_keep_their_source_and_release_with_the_claim(migrated: Any) -> None:
    db, _, nvidia = migrated
    slots = importlib.import_module("factory_turso_slots")
    holders = slots.slot_holders(REPO, 12)
    assert [(h["slot"], h["worker"], h["source"]) for h in holders] == [
        ("attempt-1", nvidia[1], "nvidia")
    ]
    assert leases.release_lease(REPO, nvidia[1], support.SLOT, "done")
    assert not db.read("SELECT 1 AS x FROM factory_lease WHERE claim_id = :c", {"c": support.SLOT})


def test_backoffs_export_like_native_rows(migrated: Any) -> None:
    found = exports.export_backoffs(REPO)["providers"]
    fib = {(row["kind"], row["model"]) for row in found["nvidia"]["fibonacci"]}
    assert fib == {("worker", "m1"), ("model", support.MODEL)}
    assert found["groq"]["capacity"][0]["scope"] == "account"
    assert found["groq"]["capacity"][0]["kind"] == "daily"
    assert found["kilo"]["capacity"][0]["scope"] == "pool"
    assert [row["model"] for row in found["nvidia"]["health"]] == ["cool-model"]


def test_the_backoff_state_round_trips_the_neon_payload(migrated: Any) -> None:
    db, data, nvidia = migrated
    key = f"factory-worker-backoff-v1:{REPO}:worker:{nvidia[0]}"
    neon = next(r for r in data["factory_state_kv"] if r["cache_key"] == key)
    (row,) = db.read("SELECT * FROM factory_backoff WHERE backoff_key = :k", {"k": key})
    assert row["worker"] == nvidia[0]
    stored = backoff.state(row)
    original = json.loads(neon["payload"])[0]
    retry = datetime.fromisoformat(original.pop("next_retry_at")).replace(microsecond=0)
    assert datetime.fromisoformat(stored.pop("next_retry_at")) == retry.astimezone(UTC)
    assert stored == original


def test_model_exclusions_and_candidate_health_include_migrated_rows(migrated: Any) -> None:
    exclusions = exports.export_model_exclusions(REPO)
    assert support.MODEL in exclusions["nvidia"] and "cool-model" in exclusions["nvidia"]
    assert exclusions["@credit_pool"] == ["kilo"]
    health = exports.export_candidate_health(REPO)
    assert {m["model"] for m in health["models"]} == {"cool-model", "ok-model"}
    assert [p["source"] for p in health["providers"]] == ["groq"]


def test_dispatch_eligibility_reads_the_migrated_worker_backoff(migrated: Any) -> None:
    db, _, nvidia = migrated
    kept, rejected = operations.eligibility(db, REPO, [nvidia[0], nvidia[2]])
    assert kept == [nvidia[2]]
    assert rejected and rejected[0].startswith(f"{nvidia[0]}: Fibonacci retry deferred")


def test_todays_migrated_assignment_counts_toward_the_daily_cap(migrated: Any) -> None:
    assert operations.daily_usage(REPO, ["nvidia"]) == {"nvidia": 1}
    db, _, _ = migrated
    (event,) = db.read("SELECT * FROM factory_state_events WHERE event_id = 'neon:1'")
    assert (event["origin"], event["source"], event["claim_id"]) == (
        "github-actions",
        "nvidia",
        support.CLAIM,
    )
    assert json.loads(event["detail"])["repo"] == REPO


def test_the_health_reset_time_bounds_the_failure_history(migrated: Any) -> None:
    db, data, _ = migrated
    neon = next(
        r for r in data["factory_state_kv"] if r["cache_key"].startswith(mapping.HEALTH_RESET)
    )
    (row,) = db.read("SELECT health_reset_at FROM factory_repo_state WHERE repo = :r", {"r": REPO})
    assert row["health_reset_at"] == int(neon["updated_at"].timestamp())
    assert runtime._history(db, REPO, 999, "nvidia", "m1", None) == 0
