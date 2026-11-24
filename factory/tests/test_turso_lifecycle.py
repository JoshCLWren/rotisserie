"""Lease lifecycle, projections, retention and backup on the libSQL store."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests import turso_support as support

leases = importlib.import_module("factory_turso_leases")
external = importlib.import_module("factory_turso_external")
events = importlib.import_module("factory_turso_events")
operations = importlib.import_module("factory_turso_operations")
exports = importlib.import_module("factory_turso_exports")
backup = importlib.import_module("factory_turso_backup")
TARGET = 77


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def test_expired_lease_is_reassigned_and_the_old_completion_cannot_touch_the_new_claim(env) -> None:
    db, repo = env
    old_worker, new_worker = support.uncapped_workers(2)
    old = leases.acquire_lease(repo, old_worker, [TARGET])
    assert old and leases.acquire_lease(repo, new_worker, [TARGET]) is None
    support.expire(db, repo, old)
    new = leases.acquire_lease(repo, new_worker, [TARGET])
    assert new and new != old
    leases.release_lease(repo, old_worker, old, "late completion")
    assert leases.target_lease(repo, TARGET)["claim_id"] == new
    assert leases.release_lease(repo, new_worker, new, "done") is True
    assert leases.release_lease(repo, new_worker, new, "done again") is False
    assert (
        support.count(
            db, "SELECT count(*) AS n FROM factory_provider_reservation WHERE repo = :r", r=repo
        )
        == 0
    )


def test_renewal_extends_only_live_verified_claims(env) -> None:
    db, repo = env
    live, lapsed = support.uncapped_workers(2)
    assert leases.acquire_lease(repo, live, [TARGET], ttl=60)
    stale = leases.acquire_lease(repo, lapsed, [TARGET + 1], ttl=60)
    support.expire(db, repo, stale)
    assert leases.renew_verified_workers(repo, [live, lapsed]) == 1
    rows = {r["key"].rsplit(":", 1)[-1]: r["active"] for r in leases.list_leases(repo)}
    assert rows[live] is True and rows[lapsed] is False


def test_worker_started_event_renews_once(env) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    token = leases.acquire_lease(repo, worker, [TARGET], ttl=60)
    assert events.record_lifecycle(repo, "31", worker, "started") == 1
    assert events.record_lifecycle(repo, "31", worker, "started") == 0
    expires = db.read(
        "SELECT expires_at - unixepoch() AS left FROM factory_lease"
        " WHERE claim_id = :c AND kind = 'worker'",
        {"c": token},
    )[0]["left"]
    assert expires > 600


def test_operation_release_cannot_remove_a_worker_claim(env) -> None:
    _, repo = env
    worker = support.uncapped_workers(1)[0]
    op = leases.acquire_operation_lease(repo, "branch-sync", TARGET)
    assert op and leases.acquire_lease(repo, worker, [TARGET]) is None
    assert leases.release_operation_lease(repo, TARGET, op, "synced") is True
    token = leases.acquire_lease(repo, worker, [TARGET])
    assert leases.release_operation_lease(repo, TARGET, token, "wrong kind") is False
    assert leases.target_lease(repo, TARGET)["claim_id"] == token


def test_external_claim_lifecycle_is_fenced_by_identity(env) -> None:
    db, repo = env
    now = datetime.now(UTC).isoformat()
    claim = external.reserve_external(
        repo,
        "alice",
        "cli",
        [TARGET],
        target=f"issue:{TARGET}",
        action="implement",
        head=None,
        observed_wip=0,
        wip_limit=2,
        offer_at=now,
    )
    assert claim["status"] == "reserved"
    token = claim["claim_id"]
    assert external.external_wip_count(repo) == 1
    assert external.external_holder(repo, TARGET)["actor"] == "alice"
    support.expire(db, repo, token)
    assert leases.acquire_lease(repo, support.uncapped_workers(1)[0], [TARGET]) is None
    assert leases.release_expired_leases(repo) == 0
    renewed = external.external_lease_action(repo, "alice", "cli", token, operation="renew")
    assert renewed == {"status": "conflict", "reason": "lease_expired_liveness_unknown"}
    wrong = external.external_lease_action(repo, "bob", "cli", token, operation="release")
    assert wrong["reason"] == "stale_or_wrong_client"
    second = external.reserve_external(
        repo,
        "carol",
        "cli",
        [TARGET + 1],
        target=f"issue:{TARGET + 1}",
        action="implement",
        head=None,
        observed_wip=1,
        wip_limit=2,
        offer_at=now,
    )
    assert second["reason"] == "wip_capacity_changed"

    def failing(_: dict) -> None:
        raise RuntimeError("GitHub evidence missing")

    with pytest.raises(RuntimeError):
        external.external_lease_action(
            repo, "alice", "cli", token, operation="handoff", evidence="pr #1", verify=failing
        )
    assert external.external_holder(repo, TARGET)["claim_id"] == token
    done = external.external_lease_action(
        repo, "alice", "cli", token, operation="handoff", evidence="pr #1", verify=lambda _: None
    )
    assert done["handoff_recorded"] is True
    assert external.external_holder(repo, TARGET) is None


def test_claim_revoked_during_verification_is_not_handed_off(env) -> None:
    _, repo = env
    claim = external.reserve_external(
        repo,
        "alice",
        "cli",
        [TARGET],
        target=f"issue:{TARGET}",
        action="review",
        head=None,
        observed_wip=0,
        wip_limit=5,
        offer_at=datetime.now(UTC).isoformat(),
    )

    def revoke_meanwhile(_: dict) -> None:
        external.revoke_external(repo, "owner", claim["claim_id"], reason="taking over")

    result = external.external_lease_action(
        repo,
        "alice",
        "cli",
        claim["claim_id"],
        operation="handoff",
        evidence="pr #2",
        verify=revoke_meanwhile,
    )
    assert result["reason"] == "claim_changed_during_verification"


def test_runtime_streaks_grow_in_order_and_older_runs_never_overwrite(env) -> None:
    db, repo = env
    worker = support.workers_by_source()["nvidia"][0]
    failure = {
        "worker": worker,
        "source": "nvidia",
        "model": "m",
        "outcome_class": "work_failure",
        "exact_model_proven": True,
    }
    for run in (101, 102, 103):
        assert operations.record_runtime(repo, run, failure) is True
    assert operations.record_runtime(repo, 103, failure) is False
    rows = db.read(
        "SELECT kind, streak, run_id FROM factory_backoff WHERE repo = :r ORDER BY kind",
        {"r": repo},
    )
    assert rows == [
        {"kind": "model", "streak": 3, "run_id": 103},
        {"kind": "worker", "streak": 3, "run_id": 103},
    ]
    kept, rejected = operations.filter_workers(repo, [worker])
    assert kept == [] and "Fibonacci" in rejected[0]
    success = {**failure, "outcome_class": "success", "exact_model_proven": True}
    assert operations.record_runtime(repo, 104, success) is True
    assert db.read("SELECT count(*) AS n FROM factory_backoff WHERE repo = :r", {"r": repo}) == [
        {"n": 0}
    ]
    health = exports.export_candidate_health(repo)["models"]
    assert [(m["model"], m["status"]) for m in health] == [("m", "healthy")]


def test_quota_refusal_sets_a_cooldown_that_a_later_refusal_never_shortens(env) -> None:
    db, repo = env
    worker = support.workers_by_source()["nvidia"][0]
    quota = {"worker": worker, "source": "nvidia", "model": "q", "outcome_class": "provider_quota"}
    operations.record_runtime(repo, 201, quota)
    first = db.read("SELECT cooldown_until FROM factory_model_health WHERE repo = :r", {"r": repo})[
        0
    ]["cooldown_until"]
    operations.record_runtime(repo, 202, quota)
    second = db.read(
        "SELECT cooldown_until FROM factory_model_health WHERE repo = :r", {"r": repo}
    )[0]["cooldown_until"]
    assert first and second >= first
    assert exports.reset_quota_backoffs(repo)["quota_health_rows"] == 1


def test_review_failure_ingestion_counts_each_run_once(env) -> None:
    _, repo = env
    event = {
        "workflow_run": {
            "head_sha": "a" * 40,
            "status": "completed",
            "conclusion": "failure",
            "name": "Local Ollama Review",
            "id": 9,
            "pull_requests": [{"number": 12}],
        }
    }
    assert operations.ingest_check(repo, event) == 1
    assert operations.ingest_check(repo, event) == 0
    assert operations.cooling_prs(repo, {12: ("a" * 40, "factory:review")}) == {12}
    assert operations.cooling_prs(repo, {12: ("b" * 40, "factory:review")}) == set()


def test_old_model_attempts_roll_into_daily_counts_once(env) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    events.record_events(
        repo,
        [
            {
                "event_type": "model_attempt",
                "event_id": f"old:{n}",
                "worker": worker,
                "source": "nvidia",
                "model": "m",
                "outcome_class": "success",
                "duration_ms": 10,
            }
            for n in range(5)
        ],
    )
    db.batch(
        [
            (
                "UPDATE factory_state_events SET created_at = unixepoch() - 200 * 86400"
                " WHERE repo = :r",
                {"r": repo},
            )
        ]
    )
    assert events.rollup_model_attempts(repo) == 5
    assert events.rollup_model_attempts(repo) == 0
    assert db.read(
        "SELECT attempts, timed_attempts, duration_ms FROM factory_model_attempt_daily"
        " WHERE repo = :r",
        {"r": repo},
    ) == [{"attempts": 5, "timed_attempts": 5, "duration_ms": 50}]


def test_backup_exports_a_portable_file_that_restores_intact(env, tmp_path: Path) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    token = leases.acquire_lease(repo, worker, [TARGET])
    counts = backup.export(db, tmp_path / "backup.sqlite", repo=repo)
    assert counts["factory_lease"] == 2 and counts["factory_state_events"] == 1
    restored = support.libsql.FileStore(tmp_path / "restored.sqlite")
    assert backup.restore(tmp_path / "backup.sqlite", restored) == counts
    rows = restored.read("SELECT claim_id FROM factory_lease WHERE kind = 'worker'")
    assert rows == [{"claim_id": token}]
    with pytest.raises(RuntimeError, match="not empty"):
        backup.restore(tmp_path / "backup.sqlite", restored)


def test_state_survives_a_new_connection(env) -> None:
    _, repo = env
    worker = support.uncapped_workers(1)[0]
    token = leases.acquire_lease(repo, worker, [TARGET])
    support.core.use_store(None)
    assert leases.worker_claim(repo, worker) == (token, [TARGET])


def test_only_one_handoff_verifies_at_a_time(env) -> None:
    _, repo = env
    claim = external.reserve_external(
        repo,
        "alice",
        "cli",
        [TARGET],
        target=f"issue:{TARGET}",
        action="review",
        head=None,
        observed_wip=0,
        wip_limit=5,
        offer_at=datetime.now(UTC).isoformat(),
    )
    token = claim["claim_id"]
    seen: list[dict] = []

    def second_handoff(_: dict) -> None:
        seen.append(
            external.external_lease_action(
                repo,
                "alice",
                "cli",
                token,
                operation="handoff",
                evidence="pr #3",
                verify=lambda _: None,
            )
        )

    done = external.external_lease_action(
        repo, "alice", "cli", token, operation="handoff", evidence="pr #3", verify=second_handoff
    )
    assert seen == [{"status": "conflict", "reason": "handoff_in_progress"}]
    assert done["handoff_recorded"] is True
