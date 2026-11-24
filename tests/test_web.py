"""Local web-control-room contract and security tests."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from rotisserie import cli, web, web_jobs


@pytest.mark.parametrize(
    ("name", "query", "expected"),
    [
        ("status", {}, ["status"]),
        ("providers", {}, ["providers"]),
        ("models", {"provider": "opencode-go"}, ["models", "opencode-go"]),
        ("why", {"target": "issue:123"}, ["why", "issue:123"]),
        ("checks", {"target": "pr:45"}, ["checks", "pr:45"]),
        ("checks", {"target": "main"}, ["checks", "main"]),
        ("follow", {"target": "pr:7"}, ["follow", "pr:7", "--once"]),
        ("follow", {"target": "issue:8"}, ["follow", "issue:8", "--once"]),
        ("runs", {"target": "issue:8"}, ["runs", "issue:8"]),
        ("history", {"target": "pr:7"}, ["history", "pr:7"]),
        ("unstick", {}, ["unstick", "--dry-run"]),
        ("stats", {"window": "24h"}, ["stats", "--window", "24h"]),
        ("outcomes", {}, ["outcomes", "--since", "2h", "--limit", "40"]),
    ],
)
def test_read_commands_are_bounded(name: str, query: dict[str, str], expected: list[str]) -> None:
    assert web.read_command(name, query) == expected


@pytest.mark.parametrize(
    ("name", "query"),
    [
        ("run", {}),
        ("settings", {"name": "FACTORY_LIMIT", "value": "20"}),
        ("models", {"provider": "--dangerous"}),
        ("why", {"target": "pr:1"}),
        ("checks", {"target": "issue:1"}),
        ("checks", {"target": "branch:main"}),
        ("checks", {"target": "main;rm"}),
        ("review", {"target": "main"}),
        ("follow", {}),
        ("follow", {"target": "pr:1", "interval": "1"}),
        ("runs", {"target": "main"}),
        ("history", {"target": "issue:0"}),
        ("stats", {"window": "30d"}),
    ],
)
def test_reads_refuse_mutations_or_invalid_args(name: str, query: dict[str, str]) -> None:
    with pytest.raises(web.InvalidRequest):
        web.read_command(name, query)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"operation": "claim", "target": "issue:12"}, ["claim", "issue:12", "--no-wait"]),
        ({"operation": "finish", "target": "pr:13"}, ["finish", "pr:13", "--no-wait"]),
        ({"operation": "labels"}, ["labels", "--via", "github", "--no-wait"]),
        (
            {"operation": "labels", "mode": "dry-run"},
            ["labels", "--via", "github", "--no-wait"],
        ),
        (
            {"operation": "labels", "mode": "repair"},
            ["labels", "--repair", "--via", "github", "--no-wait"],
        ),
    ],
)
def test_claim_finish_and_label_doctor_writes_return_at_once(
    body: dict[str, Any], expected: list[str]
) -> None:
    args = web.write_command(body)
    assert args == expected
    cli.parser().parse_args(["factory", *args])


def test_page_has_a_control_for_each_new_operation() -> None:
    page = "\n".join(
        (Path(web.__file__).parent / name).read_text(encoding="utf-8")
        for name in ("web_ui.html", "web_ui.js")
    )
    for marker in (
        'data-item-read="follow"',
        'data-item-read="runs"',
        'data-item-read="history"',
        'id="item-claim"',
        'id="item-finish"',
        'id="main-checks"',
        'id="labels-dry-run"',
        'id="labels-repair"',
        'id="provider-switches"',
        'operation:"labels",mode:"dry-run"',
        'operation:"labels",mode:"repair"',
        '"model_switch":"provider_switch"',
        "data-switch-off",
        "data-switch-on",
        "window.confirm(",
        'id="incident-start"',
        'operation:"incident_start"',
        'operation:"incident_stop"',
        'get("incident")',
    ):
        assert marker in page, marker


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
        "run",
        "opencode-go",
        "opencode-go/hy4-flash-free",
        "--no-wait",
        "--yes",
        "--target",
        "pr:123",
        "--action",
        "ci-repair",
        "--force",
        "--take-over",
        "--slot",
        "2",
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "run", "provider": "opencode-go;echo", "model": "auto"},
        {"operation": "run", "provider": "opencode-go", "model": "auto", "take_over": True},
        {
            "operation": "run",
            "provider": "opencode-go",
            "model": "auto",
            "target": "issue:1",
            "action": "review",
        },
        {"operation": "run", "provider": "opencode-go", "model": "auto", "slot": -2},
        {"operation": "cancel", "target": "pr:0"},
        {"operation": "sync", "target": "issue:2"},
        {"operation": "settings", "name": "FAKE;rm", "value": "1"},
        {"operation": "settings", "name": "FACTORY_LIMIT", "value": "1;rm"},
        {"operation": "close", "target": "pr:1"},
        {"operation": "claim", "target": "main"},
        {"operation": "finish", "target": "issue:-1"},
        {"operation": "finish"},
        {"operation": "labels", "mode": "--repair"},
        {"operation": "labels", "mode": "audit"},
    ],
)
def test_writes_refuse_unsupported_or_unsafe_inputs(body: dict[str, Any]) -> None:
    with pytest.raises(web.InvalidRequest):
        web.write_command(body)


@pytest.mark.parametrize("disconnect", [BrokenPipeError, ConnectionResetError])
def test_browser_disconnect_after_factory_response_is_not_server_error(
    disconnect: type[OSError],
) -> None:
    """The client may leave while a slow CLI call is still running."""

    class DisconnectedWriter:
        def write(self, data: bytes) -> None:
            raise disconnect("Browser has closed the connection")

    handler_type = web.make_handler("acme/widgets", "unit-test-session")
    handler = handler_type.__new__(handler_type)
    handler.wfile = DisconnectedWriter()  # type: ignore[assignment]
    setattr(handler, "_headers", lambda *_args: None)
    getattr(handler, "_send_json")(200, {"exit_code": 0, "result": "completed"})
    getattr(handler, "_write_body")(b"html response")


def test_unexpected_response_write_failure_is_not_suppressed() -> None:
    class FailingWriter:
        def write(self, data: bytes) -> None:
            raise OSError("Unexpected I/O failure")

    handler_type = web.make_handler("acme/widgets", "unit-test-session")
    handler = handler_type.__new__(handler_type)
    handler.wfile = FailingWriter()  # type: ignore[assignment]
    setattr(handler, "_headers", lambda *_args: None)

    with pytest.raises(OSError, match="Unexpected I/O failure"):
        getattr(handler, "_send_json")(200, {"result": "failed"})


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

    def command(repo: str, argv: list[str]) -> list[str]:
        calls.append((repo, argv))
        return [sys.executable, "-c", 'print(\'{"result": "completed"}\')']

    jobs = web_jobs.Jobs("acme/widgets", command=command)
    handler = web.make_handler("acme/widgets", "unit-test-session", runner=runner, jobs=jobs)
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
        connection.request(
            method, path, body=json.dumps(data) if data is not None else None, headers=merged
        )
        response = connection.getresponse()
        text = response.read().decode()
        status = response.status
        connection.close()
        return status, text

    try:
        assert send("GET", "/api/status")[0] == 403
        auth = {"X-Rotisserie-Token": "unit-test-session"}
        foreign_origin = {**auth, "Origin": "https://other.test"}
        assert send("GET", "/api/status", headers=foreign_origin)[0] == 403
        assert send("GET", "/api/status", headers={**auth, "Host": "localhost"})[0] == 403
        assert send("GET", "/api/status", headers=auth)[0] == 200
        assert calls == [("acme/widgets", ["status"])]
        page_status, page = send("GET", "/")
        assert page_status == 200
        assert "unit-test-session" in page
        assert "acme/widgets" in page
        status, body = send(
            "POST",
            "/api/command",
            {"operation": "run", "provider": "opencode-go", "model": "auto"},
            auth,
        )
        assert status == 202
        job = json.loads(body)["job"]
        assert send("GET", f"/api/jobs/{job}")[0] == 403
        view: dict[str, Any] = {"state": "running"}
        deadline = time.monotonic() + 10
        while view["state"] == "running" and time.monotonic() < deadline:
            view = json.loads(send("GET", f"/api/jobs/{job}", headers=auth)[1])
        assert view["state"] == "finished"
        assert view["result"] == {"result": "completed", "exit_code": 0}
        assert calls[-1][1] == ["run", "opencode-go", "auto", "--no-wait", "--yes"]
        assert (
            send("POST", "/api/command", {"operation": "close", "target": "pr:1"}, auth)[0] == 400
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
