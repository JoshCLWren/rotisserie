"""Faults on the libSQL store: unavailable, quota BLOCKED, lost responses, contention, rollback.

Server cases use a local fault proxy in front of a real libSQL server, so they
run only when ``FACTORY_TEST_LIBSQL_URL`` names a local server. The hosted
self-test cannot induce these faults on Turso Cloud and skips them.
"""

from __future__ import annotations

import importlib
import json
import os
import shlex
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from tests import turso_support as support

leases = importlib.import_module("factory_turso_leases")
libsql = support.libsql
core = support.core
TARGET = 31
LOCAL_SERVER = support.SERVER_URL.startswith(("http://127.0.0.1", "http://localhost"))
needs_local_server = pytest.mark.skipif(not LOCAL_SERVER, reason="needs a local libSQL server")


@pytest.fixture
def file_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo, tmp_path / "state.sqlite"


@pytest.fixture
def proxied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proxy = support.FaultProxy(support.SERVER_URL)
    monkeypatch.setattr(support, "SERVER_URL", proxy.url)
    try:
        with support.bound("server", tmp_path / "unused", monkeypatch) as (db, repo):
            yield db, repo, proxy
    finally:
        proxy.close()


def _rows(db, repo: str) -> int:
    return sum(
        support.count(db, f"SELECT count(*) AS n FROM {table} WHERE repo = :r", r=repo)
        for table in support.TABLES
    )


def test_unreachable_store_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_TURSO_URL", f"http://127.0.0.1:{support.free_port()}")
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    core.use_store(None)
    worker = support.uncapped_workers(1)[0]
    try:
        with pytest.raises(libsql.StoreUnavailable):
            leases.acquire_lease(support.repo_name(), worker, [TARGET])
    finally:
        core.use_store(None)


def test_unconfigured_store_refuses_when_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    monkeypatch.setenv("FACTORY_STATE_REQUIRED", "true")
    with pytest.raises(RuntimeError):
        leases.acquire_lease("acme/widgets", support.uncapped_workers(1)[0], [TARGET])


def test_a_failed_statement_rolls_back_the_whole_acquisition(file_env, monkeypatch) -> None:
    db, repo, _ = file_env
    original = leases.claim_statements

    def failing(*args, **kwargs):
        return [*original(*args, **kwargs)[:-1], ("INSERT INTO no_such_table VALUES (1)", {})]

    monkeypatch.setattr(leases, "claim_statements", failing)
    with pytest.raises(libsql.StoreError):
        leases.acquire_lease(repo, support.uncapped_workers(1)[0], [TARGET])
    assert _rows(db, repo) == 0


def test_write_lock_contention_waits_then_succeeds(file_env) -> None:
    db, repo, path = file_env
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.3, lambda: holder.execute("ROLLBACK"))
    release.start()
    try:
        assert leases.acquire_lease(repo, support.uncapped_workers(1)[0], [TARGET])
    finally:
        release.join()
        holder.close()


def test_contention_past_the_retry_budget_raises_and_writes_nothing(file_env, monkeypatch) -> None:
    _, repo, path = file_env
    monkeypatch.setattr(libsql, "RETRIES", 2)
    core.use_store(libsql.FileStore(path, busy_ms=20))
    worker = support.uncapped_workers(1)[0]
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(libsql.StoreContention):
            leases.acquire_lease(repo, worker, [TARGET])
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert leases.worker_claim(repo, worker) is None


@needs_local_server
def test_server_rolls_back_a_transaction_held_past_its_timeout(proxied) -> None:
    db, repo, _ = proxied
    worker, intruder = support.uncapped_workers(2)
    request = urllib.request.Request(
        support.SERVER_URL + "/v2/pipeline",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "baton": None,
                "requests": [
                    {"type": "execute", "stmt": {"sql": "BEGIN IMMEDIATE"}},
                    {
                        "type": "execute",
                        "stmt": {
                            "sql": (
                                "INSERT INTO factory_lease (lease_key, repo, kind, claim_id, acquired_at,"
                                f" expires_at) VALUES ('factory-lease-v1:{repo}:target:{TARGET}', '{repo}',"
                                " 'target', 'held', unixepoch(), unixepoch() + 999)"
                            )
                        },
                    },
                ],
            }
        ).encode(),
    )
    baton = json.loads(urllib.request.urlopen(request, timeout=10).read())["baton"]
    started = time.monotonic()
    token = leases.acquire_lease(repo, worker, [TARGET])
    assert token and time.monotonic() - started < 9
    assert leases.target_lease(repo, TARGET)["claim_id"] == token
    finish = urllib.request.Request(
        support.SERVER_URL + "/v2/pipeline",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "baton": baton,
                "requests": [{"type": "execute", "stmt": {"sql": "COMMIT"}}, {"type": "close"}],
            }
        ).encode(),
    )
    with pytest.raises(Exception):  # noqa: B017 - any refusal proves the stale stream is dead
        body = json.loads(urllib.request.urlopen(finish, timeout=10).read())
        assert all(item["type"] == "ok" for item in body["results"])
    assert leases.acquire_lease(repo, intruder, [TARGET]) is None


@needs_local_server
def test_lost_response_after_commit_reconciles_by_claim_identity(proxied) -> None:
    db, repo, proxy = proxied
    worker = support.uncapped_workers(1)[0]
    before = proxy.forwarded
    proxy.mode = "drop"
    token = leases.acquire_lease(repo, worker, [TARGET])
    assert token and proxy.forwarded - before >= 2
    assert (
        support.count(
            db,
            "SELECT count(*) AS n FROM factory_lease WHERE repo = :r AND kind = 'worker'",
            r=repo,
        )
        == 1
    )
    assert (
        support.count(db, "SELECT count(*) AS n FROM factory_state_events WHERE repo = :r", r=repo)
        == 1
    )
    proxy.mode = "drop"
    assert leases.release_lease(repo, worker, token, "done") is True
    assert (
        support.count(
            db, "SELECT count(*) AS n FROM factory_provider_reservation WHERE repo = :r", r=repo
        )
        == 0
    )


@needs_local_server
def test_exhausted_quota_pauses_without_writing(proxied) -> None:
    db, repo, proxy = proxied
    worker = support.uncapped_workers(1)[0]
    proxy.mode = "blocked"
    with pytest.raises(libsql.StoreBlocked):
        leases.acquire_lease(repo, worker, [TARGET])
    with pytest.raises(libsql.StoreBlocked):
        leases.renew_verified_workers(repo, [worker])
    proxy.mode = "pass"
    assert _rows(db, repo) == 0


@pytest.mark.skipif(
    not os.environ.get("FACTORY_TEST_LIBSQL_RESTART_CMD"),
    reason="needs FACTORY_TEST_LIBSQL_RESTART_CMD to restart the local libSQL server",
)
def test_claims_survive_a_server_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.bound("server", tmp_path / "unused", monkeypatch) as (_, repo):
        worker, other = support.uncapped_workers(2)
        token = leases.acquire_lease(repo, worker, [TARGET])
        command = shlex.split(os.environ["FACTORY_TEST_LIBSQL_RESTART_CMD"])
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        claim = None
        for _ in range(50):
            try:
                core.use_store(None)
                claim = leases.worker_claim(repo, worker)
                break
            except libsql.StoreUnavailable:
                time.sleep(0.2)
        assert claim == (token, [TARGET])
        assert leases.acquire_lease(repo, other, [TARGET]) is None


@needs_local_server
def test_every_request_is_self_contained_so_no_transaction_outlives_it(
    proxied, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, repo, _ = proxied
    bodies: list[dict] = []
    original = urllib.request.urlopen

    def recording(request, *args, **kwargs):  # type: ignore[no-untyped-def]
        bodies.append(json.loads(request.data))
        return original(request, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", recording)
    worker = support.uncapped_workers(1)[0]
    token = leases.acquire_lease(repo, worker, [TARGET])
    leases.renew_verified_workers(repo, [worker])
    leases.release_lease(repo, worker, token, "done")
    assert bodies
    for body in bodies:
        assert body["baton"] is None
        assert body["requests"][-1] == {"type": "close"}
        for item in body["requests"]:
            if item["type"] == "batch":
                steps = [step["stmt"]["sql"] for step in item["batch"]["steps"]]
                assert steps[0] == "BEGIN IMMEDIATE"
                assert steps[-2:] == ["COMMIT", "ROLLBACK"]
            else:
                assert item["type"] in ("execute", "close")
                sql = item.get("stmt", {}).get("sql", "").lstrip().upper()
                assert not sql.startswith(("BEGIN", "INSERT", "UPDATE", "DELETE"))
