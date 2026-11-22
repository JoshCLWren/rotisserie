"""Local web-control-room contract and security tests."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from rotisserie import cli, web


@pytest.mark.parametrize(
    ("name", "query", "expected"),
    [
        ("status", {}, ["status"]),
        ("providers", {}, ["providers"]),
        ("models", {"provider": "opencode-go"}, ["models", "opencode-go"]),
        ("why", {"target": "issue:123"}, ["why", "issue:123"]),
        ("checks", {"target": "pr:45"}, ["checks", "pr:45"]),
        ("unstick", {}, ["unstick", "--dry-run"]),
        ("stats", {"window": "24h"}, ["stats", "--window", "24h"]),
        ("outcomes", {}, ["outcomes", "--since", "2h", "--limit", "40"]),
    ],
)
def test_read_commands_are_bounded(
    name: str, query: dict[str, str], expected: list[str]
) -> None:
    assert web.read_command(name, query) == expected


@pytest.mark.parametrize(
    ("name", "query"),
    [
        ("run", {}),
        ("settings", {"name": "FACTORY_LIMIT", "value": "20"}),
        ("models", {"provider": "--dangerous"}),
        ("why", {"target": "pr:1"}),
        ("checks", {"target": "issue:1"}),
        ("stats", {"window": "30d"}),
    ],
)
def test_reads_refuse_mutations_or_invalid_args(name: str, query: dict[str, str]) -> None:
    with pytest.raises(web.InvalidRequest):
        web.read_command(name, query)


def test_run_maps_to_nonblocking_console_command_without_shell() -> None:
    assert web.write_command(
        {
            "operation": "run",
            "provider": "opencode-go",
            "model": "opencode-go/hy4-flash-free",
            "target": "pr:123",
            "action": "ci-repair",
            "force": True,
            "take_over": True,
            "slot": 2,
        }
    ) == [
        "run", "opencode-go", "opencode-go/hy4-flash-free", "--no-wait", "--yes",
        "--target", "pr:123", "--action", "ci-repair", "--force", "--take-over",
        "--slot", "2",
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "run", "provider": "opencode-go;echo", "model": "auto"},
        {"operation": "run", "provider": "opencode-go", "model": "auto", "take_over": True},
        {
            "operation": "run", "provider": "opencode-go", "model": "auto",
            "target": "issue:1", "action": "review",
        },
        {"operation": "run", "provider": "opencode-go", "model": "auto", "slot": -2},
        {"operation": "cancel", "target": "pr:0"},
        {"operation": "sync", "target": "issue:2"},
        {"operation": "settings", "name": "FAKE;rm", "value": "1"},
        {"operation": "settings", "name": "FACTORY_LIMIT", "value": "1;rm"},
        {"operation": "close", "target": "pr:1"},
    ],
)
def test_writes_refuse_unsupported_or_unsafe_inputs(body: dict[str, Any]) -> None:
    with pytest.raises(web.InvalidRequest):
        web.write_command(body)


def test_entrypoint_exposes_local_web_command() -> None:
    args = cli.parser().parse_args(
        ["web", "--repo", "acme/widgets", "--port", "9000", "--no-browser"]
    )
    assert args.command == "web"
    assert args.repo == "acme/widgets"
    assert args.port == 9000
    assert args.no_browser is True


def test_browser_requires_local_origin_session_and_bounded_operations() -> None:
    calls: list[tuple[str, list[str]]] = []

    def runner(repo: str, argv: list[str]) -> dict[str, Any]:
        calls.append((repo, argv))
        return {"exit_code": 0, "result": "completed", "command": argv[0]}

    handler = web.make_handler("acme/widgets", "unit-test-session", runner=runner)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def send(
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        merged = {"Host": f"127.0.0.1:{server.server_port}"}
        merged.update(headers or {})
        if data is not None:
            merged["Content-Type"] = "application/json"
        connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=merged)
        response = connection.getresponse()
        text = response.read().decode()
        status = response.status
        connection.close()
        return status, text

    try:
        assert send("GET", "/api/status")[0] == 403
        auth = {"X-Rotisserie-Token": "unit-test-session"}
        assert send("GET", "/api/status", headers={**auth, "Origin": "https://other.test"})[0] == 403
        assert send("GET", "/api/status", headers={**auth, "Host": "localhost"})[0] == 403
        assert send("GET", "/api/status", headers=auth)[0] == 200
        assert calls == [("acme/widgets", ["status"])]
        page_status, page = send("GET", "/")
        assert page_status == 200
        assert "unit-test-session" in page
        assert "acme/widgets" in page
        status, body = send(
            "POST", "/api/command",
            {"operation": "run", "provider": "opencode-go", "model": "auto"},
            auth,
        )
        assert status == 200
        assert json.loads(body)["result"] == "completed"
        assert calls[-1][1] == ["run", "opencode-go", "auto", "--no-wait", "--yes"]
        assert send(
            "POST", "/api/command",
            {"operation": "close", "target": "pr:1"}, auth
        )[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
