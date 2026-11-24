"""External claims against a real PostgreSQL server running the factory_state schema.

The fakes elsewhere prove the control flow. These prove the SQL itself: the
repository advisory lock, the never-overwrite CAS, the UUID/actor/client
fences, and the rollback of a failed handoff, under genuine concurrency.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import factory_state_ledger as ledger  # noqa: E402

BIN = next((p.parent for p in sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"))), None)
# An already running disposable server (for example a local container) may be
# supplied instead of server binaries: postgresql://postgres@127.0.0.1:PORT
EXISTING = os.environ.get("FACTORY_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(
    shutil.which("psql") is None or (BIN is None and not EXISTING),
    reason="no PostgreSQL server binaries or FACTORY_TEST_POSTGRES_URL",
)
REPO = "acme/widgets"
SCHEMA = ROOT / ".github" / "factory-state-schema.sql"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _load_schema(base: list[str]) -> None:
    for _ in range(100):
        if subprocess.run([*base, "-c", "SELECT 1"], capture_output=True).returncode == 0:
            break
        time.sleep(0.1)
    subprocess.run(
        [*base, "-c", "DROP DATABASE IF EXISTS factory_state"], check=True, capture_output=True
    )
    subprocess.run([*base, "-c", "CREATE DATABASE factory_state"], check=True, capture_output=True)
    subprocess.run([*base, "-d", "factory_state", "-f", str(SCHEMA)], check=True, capture_output=True)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if EXISTING:
        url = urlsplit(EXISTING)
        _load_schema([
            "psql", "-h", str(url.hostname), "-p", str(url.port or 5432),
            "-U", str(url.username or "postgres"), "-v", "ON_ERROR_STOP=1",
        ])
        yield EXISTING.rstrip("/") + "/factory_state"
        return
    assert BIN is not None
    root = tmp_path_factory.mktemp("pg")
    data = root / "data"
    subprocess.run(
        [str(BIN / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres", "--no-sync"],
        check=True,
        capture_output=True,
    )
    port = _free_port()
    process = subprocess.Popen(
        [
            str(BIN / "postgres"), "-D", str(data), "-k", str(root), "-p", str(port),
            "-c", "listen_addresses=127.0.0.1", "-c", "fsync=off",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = ["psql", "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-v", "ON_ERROR_STOP=1"]
    try:
        _load_schema(base)
        yield f"postgresql://postgres@127.0.0.1:{port}/factory_state"
    finally:
        process.terminate()
        process.wait(timeout=30)


@pytest.fixture
def neon(server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    dsn = tmp_path / "dsn"
    dsn.write_text(server)
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", str(dsn))
    _sql("DELETE FROM public.factory_state_kv; DELETE FROM public.factory_state_events")
    return server


def _sql(statement: str, *args: Any) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        conn = await ledger.measured_connect(dsn=ledger._dsn(), timeout=8, command_timeout=8)
        try:
            if ";" in statement and not args:
                await conn.execute(statement)
                return []
            return [dict(row) for row in await conn.fetch(statement, *args)]
        finally:
            await conn.close()

    return asyncio.run(run())


def _reserve(client: str, numbers: list[int], **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "target": f"pr:{numbers[-1]}",
        "action": "review-findings",
        "head": "a" * 40,
        "observed_wip": 0,
        "wip_limit": 8,
        "offer_at": "2026-11-22T00:00:00+00:00",
    }
    request.update(overrides)
    return ledger.reserve_external(REPO, "alice", client, numbers, **request)


def _keys() -> set[str]:
    return {str(row["cache_key"]) for row in _sql("SELECT cache_key FROM public.factory_state_kv")}


def _events() -> list[str]:
    rows = _sql("SELECT event_type FROM public.factory_state_events ORDER BY id")
    return [str(row["event_type"]) for row in rows]


def test_concurrent_clients_racing_one_offer_produce_exactly_one_claim(neon: str) -> None:
    clients = [f"agent-{n}" for n in range(8)]
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        answers = list(pool.map(lambda c: _reserve(c, [7, 44]), clients))
    winners = [a for a in answers if a["status"] == "reserved"]
    losers = [a for a in answers if a["status"] != "reserved"]
    assert len(winners) == 1
    assert {a["reason"] for a in losers} == {"existing_or_unreconciled_lease"}
    assert all(a["retry"] == "refresh_frontier" for a in losers)
    token = winners[0]["claim_id"]
    owners = _sql("SELECT DISTINCT payload->0->>'claim_id' AS id FROM public.factory_state_kv")
    assert [row["id"] for row in owners] == [token]
    assert _events() == ["external_reserved"]


def test_linked_target_cannot_be_reserved_separately(neon: str) -> None:
    assert _reserve("agent-a", [7, 44])["status"] == "reserved"
    assert _reserve("agent-b", [44])["reason"] == "existing_or_unreconciled_lease"
    assert _reserve("agent-b", [7], target="issue:7", action="implement", head=None)[
        "status"
    ] == "conflict"


def test_external_claim_blocks_factory_workers_even_after_it_expires(neon: str) -> None:
    claim = _reserve("agent-a", [7, 44])
    assert ledger.acquire_lease(REPO, "3", [7, 44], 600) is None
    _sql(
        "UPDATE public.factory_state_kv SET expires_at = now() - interval '2 hours'"
    )
    assert ledger.acquire_lease(REPO, "3", [44], 600) is None
    assert ledger.release_expired_leases(REPO) == 0
    assert len(_keys()) == 3
    assert ledger.external_lease_action(
        REPO, "alice", "agent-a", claim["claim_id"], operation="renew"
    )["reason"] == "lease_expired_liveness_unknown"
    # The owner may still give it back; then the work is free again.
    assert ledger.external_lease_action(
        REPO, "alice", "agent-a", claim["claim_id"], operation="release"
    )["status"] == "released"
    assert _keys() == set()
    assert _reserve("agent-b", [7, 44])["status"] == "reserved"


def test_an_expired_factory_lease_frees_its_target_for_an_outside_claim(neon: str) -> None:
    _sql(
        """INSERT INTO public.factory_state_kv (cache_key, payload, fetched_at, expires_at)
           VALUES ($1, $2::jsonb, now(), now() + interval '10 minutes')""",
        ledger._target_key(REPO, 7),
        '[{"claim_id": "f-1", "worker": "3", "targets": [7]}]',
    )
    assert _reserve("agent-a", [7], target="issue:7", action="implement", head=None)[
        "reason"
    ] == "existing_or_unreconciled_lease"
    _sql("UPDATE public.factory_state_kv SET expires_at = now() - interval '1 minute' RETURNING 1")
    claim = _reserve("agent-a", [7], target="issue:7", action="implement", head=None)
    assert claim["status"] == "reserved"


def test_an_expired_outside_claim_still_blocks_another_outside_claim(neon: str) -> None:
    assert _reserve("agent-a", [7, 44])["status"] == "reserved"
    _sql("UPDATE public.factory_state_kv SET expires_at = now() - interval '2 hours'")
    assert _reserve("agent-b", [7, 44])["reason"] == "existing_or_unreconciled_lease"


def test_stale_or_foreign_clients_cannot_touch_a_claim(neon: str) -> None:
    claim = _reserve("agent-a", [7, 44])
    token = claim["claim_id"]
    for actor, client in (("alice", "agent-b"), ("mallory", "agent-a")):
        for operation in ("renew", "release"):
            refused = ledger.external_lease_action(
                REPO, actor, client, token, operation=operation
            )
            assert refused == {"status": "conflict", "reason": "stale_or_wrong_client"}
    before = _sql("SELECT expires_at FROM public.factory_state_kv ORDER BY cache_key")
    renewed = ledger.external_lease_action(
        REPO, "alice", "agent-a", token, operation="renew", ttl=3600
    )
    assert renewed["status"] == "renewed"
    after = _sql("SELECT expires_at FROM public.factory_state_kv ORDER BY cache_key")
    assert all(b["expires_at"] < a["expires_at"] for b, a in zip(before, after, strict=True))
    released = ledger.external_lease_action(REPO, "alice", "agent-a", token, operation="release")
    assert released["status"] == "released"
    # An old token never touches the next holder's claim.
    newer = _reserve("agent-b", [7, 44])
    assert ledger.external_lease_action(
        REPO, "alice", "agent-a", token, operation="release"
    )["reason"] == "claim_missing_or_replaced"
    revoked = ledger.revoke_external(REPO, "josh", token, reason="stale")
    assert revoked["reason"] == "claim_missing_or_replaced"
    assert {row["id"] for row in _sql(
        "SELECT payload->0->>'claim_id' AS id FROM public.factory_state_kv"
    )} == {newer["claim_id"]}


def test_failed_handoff_verification_rolls_back_and_keeps_the_claim(neon: str) -> None:
    claim = _reserve("agent-a", [7, 44])
    token = claim["claim_id"]
    seen: list[dict[str, Any]] = []

    def refuse(record: dict[str, Any]) -> None:
        seen.append(record)
        raise ValueError("no new commit on the claimed PR")

    with pytest.raises(ValueError, match="no new commit"):
        ledger.external_lease_action(
            REPO, "alice", "agent-a", token, operation="handoff",
            evidence=f"https://github.com/{REPO}/pull/44", verify=refuse,
        )
    assert seen[0]["target"] == "pr:44" and seen[0]["head"] == "a" * 40
    assert len(_keys()) == 3
    assert _events() == ["external_reserved"]

    done = ledger.external_lease_action(
        REPO, "alice", "agent-a", token, operation="handoff",
        evidence=f"https://github.com/{REPO}/pull/44", verify=lambda record: None,
    )
    assert done["handoff_recorded"] is True
    assert _keys() == set()
    detail = _sql(
        "SELECT detail FROM public.factory_state_events WHERE event_type='external_handoff'"
    )[0]["detail"]
    record = json.loads(detail) if isinstance(detail, str) else detail
    assert record["evidence"] == f"https://github.com/{REPO}/pull/44"
    assert record["client"] == "agent-a"


def test_implementation_claims_respect_the_shared_wip_limit(neon: str) -> None:
    assert _reserve("agent-a", [44])["status"] == "reserved"
    full = _reserve(
        "agent-b", [7], target="issue:7", action="implement", head=None,
        observed_wip=7, wip_limit=8,
    )
    assert full["reason"] == "wip_capacity_changed"
    assert ledger.external_wip_count(REPO) == 1
    assert _reserve(
        "agent-b", [8], target="issue:8", action="implement", head=None,
        observed_wip=6, wip_limit=8,
    )["status"] == "reserved"
    assert ledger.external_wip_count(REPO) == 2


def test_no_credentials_leave_the_connection_file(neon: str) -> None:
    assert os.environ["FACTORY_STATE_DSN_FILE"]
    claim = _reserve("agent-a", [44])
    assert "postgres" not in json.dumps(claim)


def test_factory_intake_backs_off_when_an_outside_claim_took_the_slot(neon: str) -> None:
    """The controller planned against N outside WIP claims; N+1 now exist."""
    assert _reserve(
        "agent-a", [9], target="issue:9", action="implement", head=None
    )["status"] == "reserved"
    assert ledger.acquire_lease(REPO, "3", [12], 600, external_wip_seen=0) is None
    assert not any(":worker:" in key or key.endswith(":target:12") for key in _keys())


def test_policy_wip_exemption_reserves_an_urgent_issue_at_the_limit(neon: str) -> None:
    urgent = _reserve(
        "agent-a", [7], target="issue:7", action="implement", head=None,
        observed_wip=8, wip_limit=8, wip_exempt=True,
    )
    assert urgent["status"] == "reserved"


def test_factory_claims_after_the_snapshot_count_against_outside_intake(neon: str) -> None:
    snapshot = "2026-01-01T00:00:00+00:00"
    _sql(
        """INSERT INTO public.factory_state_kv (cache_key, payload, fetched_at, expires_at)
           VALUES ($1, '[{"claim_id": "w", "worker": "5", "targets": [70]}]'::jsonb,
                   now(), now() + interval '1 hour')""",
        f"factory-lease-v1:{REPO}:worker:5",
    )
    full = _reserve(
        "agent-a", [7], target="issue:7", action="implement", head=None,
        observed_wip=7, wip_limit=8, offer_at=snapshot,
    )
    assert full["reason"] == "wip_capacity_changed"
    room = _reserve(
        "agent-a", [7], target="issue:7", action="implement", head=None,
        observed_wip=6, wip_limit=8, offer_at=snapshot,
    )
    assert room["status"] == "reserved"


def test_handoff_refuses_an_evidence_pr_someone_else_holds(neon: str) -> None:
    claim = _reserve("agent-a", [7], target="issue:7", action="implement", head=None)
    other = _reserve("agent-b", [88])
    assert other["status"] == "reserved"
    called: list[object] = []
    refused = ledger.external_lease_action(
        REPO, "alice", "agent-a", claim["claim_id"], operation="handoff",
        evidence=f"https://github.com/{REPO}/pull/88", verify=called.append,
        evidence_target=88,
    )
    assert refused == {"status": "conflict", "reason": "evidence_target_held"}
    assert called == []
    assert any(key.endswith(":target:7") for key in _keys())


def test_factory_claims_long_before_the_snapshot_are_already_in_its_count(neon: str) -> None:
    from datetime import UTC, datetime

    _sql(
        """INSERT INTO public.factory_state_kv (cache_key, payload, fetched_at, expires_at)
           VALUES ($1, '[{"claim_id": "w", "worker": "5", "targets": [70]}]'::jsonb,
                   now() - interval '10 minutes', now() + interval '1 hour')""",
        f"factory-lease-v1:{REPO}:worker:5",
    )
    room = _reserve(
        "agent-a", [7], target="issue:7", action="implement", head=None,
        observed_wip=7, wip_limit=8, offer_at=datetime.now(UTC).isoformat(),
    )
    assert room["status"] == "reserved"
