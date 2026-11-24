"""Local, authenticated web UI adapter for the existing factory operator CLI.

No factory policy or state lives here. Requests are strict, bounded command
specifications, never shell input. The console remains the sole executor.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from rotisserie.control_contract import (
    MAX_BODY,
    READ_ONLY,
    InvalidRequest,
    read_command,
    write_command,
)
from rotisserie.factory_console.context import UsageError, find_repository

REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
__all__ = [
    "READ_ONLY",
    "InvalidRequest",
    "invoke",
    "make_handler",
    "read_command",
    "serve",
    "write_command",
]


def invoke(repository: str, args: list[str], *, timeout: int = 90) -> dict[str, Any]:
    """Call the shipped CLI, which enforces all dispatcher and lease checks."""
    command = [
        sys.executable,
        "-m",
        "rotisserie",
        "factory",
        "--repo",
        repository,
        "--json",
        *args,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {"result": "timeout", "error": "The factory command timed out.", "exit_code": 4}
    except OSError:
        return {"result": "unavailable", "error": "Could not start Rotisserie.", "exit_code": 4}
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("Not a JSON object")
    except ValueError:
        return {
            "result": "unavailable",
            "error": "The factory did not return its JSON contract.",
            "exit_code": result.returncode or 4,
        }
    payload["exit_code"] = result.returncode
    return payload


def make_handler(
    repository: str,
    token: str,
    *,
    runner: Callable[[str, list[str]], dict[str, Any]] = invoke,
) -> type[BaseHTTPRequestHandler]:
    """Construct an isolated handler, injectable without running GitHub in tests."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "RotisserieWeb/0.1"

        def _headers(self, code: int, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()

        def _write_body(self, body: bytes) -> None:
            """A browser can abandon a slow factory read before we respond.

            The operation itself has already run by this point; a disconnected
            client is not a factory failure. Preserve unexpected I/O errors.
            """
            try:
                self.wfile.write(body)
            except BrokenPipeError, ConnectionResetError:
                # A tab closed, navigated away, or discarded an old response.
                return

        def _send_json(self, code: int, data: Mapping[str, Any]) -> None:
            blob = json.dumps(data, default=str).encode("utf-8")
            self._headers(code, "application/json; charset=utf-8")
            self._write_body(blob)

        def _authorized(self) -> bool:
            host = self.headers.get("Host", "")
            allowed = f"127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}"
            origin = self.headers.get("Origin", "")
            if host != allowed or (origin and origin != f"http://{allowed}"):
                self._send_json(403, {"error": "This console is localhost-only."})
                return False
            if not secrets.compare_digest(self.headers.get("X-Rotisserie-Token", ""), token):
                self._send_json(403, {"error": "Invalid console session."})
                return False
            return True

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                # Host validation for the document prevents DNS-rebinding access.
                if (
                    self.headers.get("Host", "")
                    != f"127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}"
                ):
                    self._send_json(403, {"error": "This console is localhost-only."})
                    return
                page = files("rotisserie").joinpath("web_ui.html").read_text(encoding="utf-8")
                page = page.replace("__SESSION_TOKEN__", json.dumps(token))
                page = page.replace("__REPOSITORY__", json.dumps(repository))
                self._headers(200, "text/html; charset=utf-8")
                self._write_body(page.encode("utf-8"))
                return
            if not parsed.path.startswith("/api/") or not self._authorized():
                if parsed.path.startswith("/api/"):
                    return
                self._send_json(404, {"error": "Not found"})
                return
            try:
                name = parsed.path.removeprefix("/api/")
                if len(self.path) > 512:
                    raise InvalidRequest("Query too long")
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                args = read_command(name, query)
            except InvalidRequest as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, runner(repository, args))

        def do_POST(self) -> None:
            if self.path != "/api/command":
                self._send_json(404, {"error": "Not found"})
                return
            if not self._authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= MAX_BODY:
                    raise InvalidRequest("Request body too large or missing")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise InvalidRequest("Expected a JSON object")
                args = write_command(data)
            except (ValueError, TypeError, json.JSONDecodeError, InvalidRequest) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, runner(repository, args))

        def log_message(self, format: str, *args: object) -> None:
            # Never log requests or tokens on a shared terminal.
            return

    return Handler


def serve(repo: str | None, port: int, *, open_browser: bool = True) -> int:
    """Bind only IPv4 loopback; the browser never receives gh credentials."""
    if not 0 <= port <= 65535:
        raise UsageError("--port must be between 0 and 65535")
    repository = find_repository(repo, dict(os.environ), Path.cwd())
    if not REPO.fullmatch(repository):
        raise UsageError("Repository must have owner/name form")
    token = secrets.token_urlsafe(32)
    with ThreadingHTTPServer(("127.0.0.1", port), make_handler(repository, token)) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Rotisserie control room: {url}", flush=True)
        print(f"Repository: {repository}  |  Ctrl+C to stop", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            print("\nRotisserie control room stopped.", flush=True)
    return 0
