"""Stores and helpers for the libSQL store suites: a SQLite file, a libSQL server, faults.

Every test runs against a plain SQLite file. It also runs against a libSQL
server when ``FACTORY_TEST_LIBSQL_URL`` names one (a local ``sqld`` here, the
hosted Turso database in the self-test workflow), with an optional token in
``FACTORY_TEST_LIBSQL_TOKEN_FILE``. Each test writes only rows of its own
synthetic repository and deletes them afterwards.
"""

from __future__ import annotations

import contextlib
import http.server
import importlib
import os
import socket
import threading
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

libsql = importlib.import_module("factory_libsql")
core = importlib.import_module("factory_turso_core")

SERVER_URL = os.environ.get("FACTORY_TEST_LIBSQL_URL", "")
TOKEN_FILE = os.environ.get("FACTORY_TEST_LIBSQL_TOKEN_FILE", "")
NAMESPACE = os.environ.get("FACTORY_TEST_NAMESPACE", "rotisserie-selftest")
TABLES = (
    "factory_lease",
    "factory_provider_reservation",
    "factory_backoff",
    "factory_repo_state",
    "factory_model_health",
    "factory_pr_stage_retry",
    "factory_worker_metrics",
    "factory_runtime_event_keys",
    "factory_state_events",
    "factory_model_attempt_daily",
)


def kinds() -> list[str]:
    return ["file", "server"] if SERVER_URL else ["file"]


def repo_name() -> str:
    return f"{NAMESPACE}/t-{uuid.uuid4().hex[:12]}"


def environment(kind: str, path: Path) -> dict[str, str]:
    """Environment that points this process and its children at one store."""
    if kind == "file":
        return {"FACTORY_STATE_SQLITE": str(path), "FACTORY_TURSO_URL": ""}
    return {
        "FACTORY_TURSO_URL": SERVER_URL,
        "FACTORY_TURSO_TOKEN_FILE": TOKEN_FILE,
        "FACTORY_STATE_SQLITE": "",
    }


def cleanup(db: Any, repo: str) -> None:
    db.batch([(f"DELETE FROM {table} WHERE repo = :repo", {"repo": repo}) for table in TABLES])


@contextlib.contextmanager
def bound(kind: str, path: Path, monkeypatch: Any) -> Iterator[tuple[Any, str]]:
    for key, value in environment(kind, path).items():
        if value:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    core.use_store(None)
    db = core.store()
    repo = repo_name()
    try:
        yield db, repo
    finally:
        cleanup(db, repo)
        core.use_store(None)


def in_child(env: dict[str, str], function: Callable[..., Any], *args: Any) -> Any:
    """Run one store call in a fresh process with its own connection, as an Actions job does."""
    for key, value in env.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    core.use_store(None)
    return function(*args)


def count(db: Any, sql: str, **params: Any) -> int:
    return int(db.read(sql, params)[0]["n"])


def expire(db: Any, repo: str, claim_id: str) -> None:
    db.batch(
        [
            (
                "UPDATE factory_lease SET expires_at = unixepoch() - 1 WHERE repo = :r AND claim_id = :c",
                {"r": repo, "c": claim_id},
            )
        ]
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class FaultProxy:
    """A local HTTP proxy in front of the libSQL server that injects one fault per mode.

    ``drop`` forwards the next write batch, lets the server commit it, then
    closes the connection without a response. ``blocked`` answers like an exhausted
    quota without forwarding. ``pass`` forwards unchanged.
    """

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream.rstrip("/")
        self.mode = "pass"
        self.forwarded = 0
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if proxy.mode == "blocked":
                    message = b'{"error":"Operation was blocked: BLOCKED (quota exceeded)"}'
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(message)))
                    self.end_headers()
                    self.wfile.write(message)
                    return
                headers = {"Content-Type": "application/json"}
                if self.headers.get("Authorization"):
                    headers["Authorization"] = self.headers["Authorization"]
                request = urllib.request.Request(
                    proxy.upstream + self.path, data=body, headers=headers, method="POST"
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = response.read()
                proxy.forwarded += 1
                if proxy.mode == "drop" and b'"batch"' in body:
                    proxy.mode = "pass"
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def workers_by_source() -> dict[str, list[str]]:
    slots = importlib.import_module("factory_worker_slots").slots
    found: dict[str, list[str]] = {}
    for entry in slots():
        found.setdefault(entry.source, []).append(entry.worker)
    return found


def uncapped_workers(count_: int) -> list[str]:
    caps = importlib.import_module("factory_source_budget").load_budgets()
    pool = [w for source, ws in workers_by_source().items() if source not in caps for w in ws]
    return pool[:count_]
