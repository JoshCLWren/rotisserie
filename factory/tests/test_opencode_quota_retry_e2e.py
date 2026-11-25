"""The real OpenCode 2 binary with the quota plugin, against a local fake provider.

Opt in with FACTORY_OPENCODE_BIN=/path/to/opencode. No real provider is called.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import factory_quota_reset as reset
import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".github/scripts/opencode-factory-quota-retry.mjs"
BINARY = os.environ.get("FACTORY_OPENCODE_BIN", "")
REFUSALS = {
    "daily": (
        {
            "message": "Rate limit exceeded: free-models-per-day-high-balance. ",
            "code": 429,
            "metadata": {"headers": {"X-RateLimit-Reset": "1763769600000"}},
        },
        {"X-RateLimit-Reset": "1763769600000"},
    ),
    "minute": (
        {"message": "Rate limit exceeded: free-models-per-min. ", "code": 429},
        {"Retry-After": "2"},
    ),
}

pytestmark = pytest.mark.skipif(not BINARY, reason="set FACTORY_OPENCODE_BIN to run")


def serve(mode: str, requests: list[dict]) -> ThreadingHTTPServer:
    error, headers = REFUSALS[mode]

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            requests.append(json.loads(self.rfile.read(length) or b"{}"))
            raw = json.dumps({"error": error}).encode()
            self.send_response(429)
            self.send_header("content-type", "application/json")
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def run(tmp_path: Path) -> Iterator:
    servers: list[ThreadingHTTPServer] = []

    def go(mode: str) -> tuple[int, str, float, int]:
        requests: list[dict] = []
        server = serve(mode, requests)
        servers.append(server)
        config = tmp_path / mode / ".config/opencode"
        (config / "plugins").mkdir(parents=True)
        shutil.copy(PLUGIN, config / "plugins/factory-quota-retry.js")
        provider = {
            "package": "@opencode/ai/providers/openai-compatible",
            "env": ["FAKE_API_KEY"],
            "settings": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1"},
            "models": {"free": {"name": "free"}},
        }
        (config / "opencode.json").write_text(json.dumps({"providers": {"fake": provider}}))
        work = tmp_path / mode / "work"
        work.mkdir()
        home = str(tmp_path / mode)
        env = {
            "PATH": os.environ["PATH"],
            "HOME": home,
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_DATA_HOME": f"{home}/.local/share",
            "XDG_STATE_HOME": f"{home}/.local/state",
            "XDG_CACHE_HOME": f"{home}/.cache",
            "FAKE_API_KEY": "fake",
        }
        started = time.monotonic()
        command = [BINARY, "run", "--standalone", "-m", "fake/free", "--agent", "build", "hi"]
        done = subprocess.run(
            command, cwd=work, env=env, capture_output=True, text=True, timeout=180, check=False
        )
        elapsed = time.monotonic() - started
        # The title request carries no tools; only the session request counts.
        session_requests = sum(1 for body in requests if body.get("tools"))
        return done.returncode, done.stdout + done.stderr, elapsed, session_requests

    yield go
    for server in servers:
        server.shutdown()


def test_daily_refusal_fails_on_first_response(run, tmp_path: Path) -> None:
    status, log, elapsed, session_requests = run("daily")
    assert status != 0
    assert session_requests == 1
    assert elapsed < 20
    assert "free-models-per-day-high-balance" in log
    assert "retrying" not in log
    log_file = tmp_path / "daily.log"
    log_file.write_text(log)
    kind = subprocess.run(
        [
            "python3",
            str(Path(reset.__file__)),
            "account",
            "--source=openrouter-free",
            "--model=x",
            "--outcome=provider_quota",
            f"--log={log_file}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert kind.stdout.startswith("daily\t")


def test_minute_throttle_gets_one_retry(run) -> None:
    status, log, elapsed, session_requests = run("minute")
    assert status != 0
    assert session_requests == 2
    assert elapsed < 30
    assert "free-models-per-min" in log
