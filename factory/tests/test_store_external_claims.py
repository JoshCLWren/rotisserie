"""External claims on the factory store: fences, WIP capacity and handoff, under real processes.

Ported from the PostgreSQL suite. Each case runs on a SQLite file, and on a
libSQL server when one is configured.
"""

from __future__ import annotations

import importlib
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests import turso_support as support

ledger = importlib.import_module("factory_state_ledger")
HEAD = "a" * 40


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "state.sqlite"
    with support.bound(request.param, path, monkeypatch) as (db, repo):
        yield support.environment(request.param, path), db, repo


def _reserve(repo: str, client: str, numbers: list[int], **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "target": f"pr:{numbers[-1]}",
        "action": "review-findings",
        "head": HEAD,
        "observed_wip": 0,
        "wip_limit": 8,
        "offer_at": "2026-11-22T00:00:00+00:00",
    }
    request.update(overrides)
    return ledger.reserve_external(repo, "alice", client, numbers, **request)


def _implement(repo: str, client: str, number: int, **overrides: Any) -> dict[str, Any]:
    return _reserve(
        repo, client, [number], target=f"issue:{number}", action="implement", head=None, **overrides
    )


def _keys(db: Any, repo: str) -> set[str]:
    rows = db.read("SELECT lease_key FROM factory_lease WHERE repo = :r", {"r": repo})
    return {str(r["lease_key"]) for r in rows}


def _events(db: Any, repo: str) -> list[str]:
    rows = db.read(
        "SELECT event_type FROM factory_state_events WHERE repo = :r ORDER BY id", {"r": repo}
    )
    return [str(r["event_type"]) for r in rows]


def _expire_all(db: Any, repo: str, seconds: int) -> None:
    db.batch(
        [
            (
                "UPDATE factory_lease SET expires_at = unixepoch() - :s WHERE repo = :r",
                {"r": repo, "s": seconds},
            )
        ]
    )


def test_concurrent_clients_racing_one_offer_produce_exactly_one_claim(env) -> None:
    environment, db, repo = env
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=8, mp_context=context) as pool:
        futures = [
            pool.submit(support.in_child, environment, _reserve, repo, f"agent-{n}", [7, 44])
            for n in range(8)
        ]
        answers = [future.result() for future in futures]
    winners = [a for a in answers if a["status"] == "reserved"]
    losers = [a for a in answers if a["status"] != "reserved"]
    assert len(winners) == 1
    assert {a["reason"] for a in losers} == {"existing_or_unreconciled_lease"}
    assert all(a["retry"] == "refresh_frontier" for a in losers)
    owners = db.read("SELECT DISTINCT claim_id FROM factory_lease WHERE repo = :r", {"r": repo})
    assert [r["claim_id"] for r in owners] == [winners[0]["claim_id"]]
    assert _events(db, repo) == ["external_reserved"]


def test_linked_target_cannot_be_reserved_separately(env) -> None:
    _, _, repo = env
    assert _reserve(repo, "agent-a", [7, 44])["status"] == "reserved"
    assert _reserve(repo, "agent-b", [44])["reason"] == "existing_or_unreconciled_lease"
    assert _implement(repo, "agent-b", 7)["status"] == "conflict"


def test_external_claim_blocks_factory_workers_even_after_it_expires(env) -> None:
    _, db, repo = env
    worker = support.uncapped_workers(1)[0]
    claim = _reserve(repo, "agent-a", [7, 44])
    assert ledger.acquire_lease(repo, worker, [7, 44], 600) is None
    _expire_all(db, repo, 7200)
    assert ledger.acquire_lease(repo, worker, [44], 600) is None
    assert ledger.release_expired_leases(repo) == 0
    assert len(_keys(db, repo)) == 3
    renewed = ledger.external_lease_action(
        repo, "alice", "agent-a", claim["claim_id"], operation="renew"
    )
    assert renewed["reason"] == "lease_expired_liveness_unknown"
    released = ledger.external_lease_action(
        repo, "alice", "agent-a", claim["claim_id"], operation="release"
    )
    assert released["status"] == "released"
    assert _keys(db, repo) == set()
    assert _reserve(repo, "agent-b", [7, 44])["status"] == "reserved"


def test_an_expired_factory_lease_frees_its_target_for_an_outside_claim(env) -> None:
    _, db, repo = env
    token = ledger.acquire_lease(repo, support.uncapped_workers(1)[0], [7], 600)
    assert token
    assert _implement(repo, "agent-a", 7)["reason"] == "existing_or_unreconciled_lease"
    support.expire(db, repo, token)
    assert _implement(repo, "agent-a", 7)["status"] == "reserved"


def test_an_expired_outside_claim_still_blocks_another_outside_claim(env) -> None:
    _, db, repo = env
    assert _reserve(repo, "agent-a", [7, 44])["status"] == "reserved"
    _expire_all(db, repo, 7200)
    assert _reserve(repo, "agent-b", [7, 44])["reason"] == "existing_or_unreconciled_lease"


def test_stale_or_foreign_clients_cannot_touch_a_claim(env) -> None:
    _, db, repo = env
    token = _reserve(repo, "agent-a", [7, 44])["claim_id"]
    for actor, client in (("alice", "agent-b"), ("mallory", "agent-a")):
        for operation in ("renew", "release"):
            refused = ledger.external_lease_action(repo, actor, client, token, operation=operation)
            assert refused == {"status": "conflict", "reason": "stale_or_wrong_client"}
    db.batch(
        [
            (
                "UPDATE factory_lease SET expires_at = unixepoch() + 600 WHERE repo = :r",
                {"r": repo},
            )
        ]
    )
    order = "SELECT expires_at FROM factory_lease WHERE repo = :r ORDER BY lease_key"
    before = db.read(order, {"r": repo})
    renewed = ledger.external_lease_action(
        repo, "alice", "agent-a", token, operation="renew", ttl=3600
    )
    assert renewed["status"] == "renewed"
    after = db.read(order, {"r": repo})
    assert all(b["expires_at"] < a["expires_at"] for b, a in zip(before, after, strict=True))
    released = ledger.external_lease_action(repo, "alice", "agent-a", token, operation="release")
    assert released["status"] == "released"
    newer = _reserve(repo, "agent-b", [7, 44])
    stale = ledger.external_lease_action(repo, "alice", "agent-a", token, operation="release")
    assert stale["reason"] == "claim_missing_or_replaced"
    assert ledger.revoke_external(repo, "josh", token, reason="stale")["reason"] == (
        "claim_missing_or_replaced"
    )
    owners = db.read("SELECT DISTINCT claim_id FROM factory_lease WHERE repo = :r", {"r": repo})
    assert {r["claim_id"] for r in owners} == {newer["claim_id"]}


def test_failed_handoff_verification_rolls_back_and_keeps_the_claim(env) -> None:
    _, db, repo = env
    token = _reserve(repo, "agent-a", [7, 44])["claim_id"]
    evidence = f"https://github.com/{repo}/pull/44"
    seen: list[dict[str, Any]] = []

    def refuse(record: dict[str, Any]) -> None:
        seen.append(record)
        raise ValueError("no new commit on the claimed PR")

    with pytest.raises(ValueError, match="no new commit"):
        ledger.external_lease_action(
            repo, "alice", "agent-a", token, operation="handoff", evidence=evidence, verify=refuse
        )
    assert seen[0]["target"] == "pr:44" and seen[0]["head"] == HEAD
    assert len(_keys(db, repo)) == 3
    assert _events(db, repo) == ["external_reserved"]
    done = ledger.external_lease_action(
        repo,
        "alice",
        "agent-a",
        token,
        operation="handoff",
        evidence=evidence,
        verify=lambda record: None,
    )
    assert done["handoff_recorded"] is True
    assert _keys(db, repo) == set()
    detail = db.read(
        "SELECT detail FROM factory_state_events WHERE repo = :r"
        " AND event_type = 'external_handoff'",
        {"r": repo},
    )[0]["detail"]
    record = json.loads(detail)
    assert record["evidence"] == evidence and record["client"] == "agent-a"


def test_implementation_claims_respect_the_shared_wip_limit(env) -> None:
    _, _, repo = env
    assert _reserve(repo, "agent-a", [44])["status"] == "reserved"
    full = _implement(repo, "agent-b", 7, observed_wip=7, wip_limit=8)
    assert full["reason"] == "wip_capacity_changed"
    assert ledger.external_wip_count(repo) == 1
    assert _implement(repo, "agent-b", 8, observed_wip=6, wip_limit=8)["status"] == "reserved"
    assert ledger.external_wip_count(repo) == 2


def test_factory_intake_backs_off_when_an_outside_claim_took_the_slot(env) -> None:
    _, db, repo = env
    worker = support.uncapped_workers(1)[0]
    assert _implement(repo, "agent-a", 9)["status"] == "reserved"
    assert ledger.acquire_lease(repo, worker, [12], 600, external_wip_seen=0) is None
    assert not any(":worker:" in k or k.endswith(":target:12") for k in _keys(db, repo))


def test_policy_wip_exemption_reserves_an_urgent_issue_at_the_limit(env) -> None:
    _, _, repo = env
    urgent = _implement(repo, "agent-a", 7, observed_wip=8, wip_limit=8, wip_exempt=True)
    assert urgent["status"] == "reserved"


def test_factory_claims_after_the_snapshot_count_against_outside_intake(env) -> None:
    _, _, repo = env
    assert ledger.acquire_lease(repo, support.uncapped_workers(1)[0], [70], 3600)
    snapshot = "2026-01-01T00:00:00+00:00"
    full = _implement(repo, "agent-a", 7, observed_wip=7, wip_limit=8, offer_at=snapshot)
    assert full["reason"] == "wip_capacity_changed"
    room = _implement(repo, "agent-a", 7, observed_wip=6, wip_limit=8, offer_at=snapshot)
    assert room["status"] == "reserved"


def test_factory_claims_long_before_the_snapshot_are_already_in_its_count(env) -> None:
    _, db, repo = env
    assert ledger.acquire_lease(repo, support.uncapped_workers(1)[0], [70], 3600)
    db.batch(
        [
            (
                "UPDATE factory_lease SET acquired_at = unixepoch() - 600 WHERE repo = :r",
                {"r": repo},
            )
        ]
    )
    now = datetime.now(UTC).isoformat()
    room = _implement(repo, "agent-a", 7, observed_wip=7, wip_limit=8, offer_at=now)
    assert room["status"] == "reserved"


def test_handoff_refuses_an_evidence_pr_someone_else_holds(env) -> None:
    _, db, repo = env
    claim = _implement(repo, "agent-a", 7)
    assert _reserve(repo, "agent-b", [88])["status"] == "reserved"
    called: list[object] = []
    refused = ledger.external_lease_action(
        repo,
        "alice",
        "agent-a",
        claim["claim_id"],
        operation="handoff",
        evidence=f"https://github.com/{repo}/pull/88",
        verify=called.append,
        evidence_target=88,
    )
    assert refused == {"status": "conflict", "reason": "evidence_target_held"}
    assert called == []
    assert any(k.endswith(":target:7") for k in _keys(db, repo))
