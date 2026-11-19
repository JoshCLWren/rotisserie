#!/usr/bin/env python3
"""Network-free tests for trusted GitHub App client-ID lookup."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

app_client = importlib.import_module("factory_app_client_id")


class Response:
    def __init__(self, data: str):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data.encode("utf-8")


def test_resolves_exact_authenticated_app_without_exposing_key() -> None:
    requests = []

    def open_app(request, *, timeout=30):
        requests.append((request, timeout))
        return Response('{"id": 123, "client_id": "Iv1.test123456"}')

    with mock.patch.object(app_client, "build_app_jwt", return_value="trusted.jwt"):
        client_id = app_client.resolve_client_id("123", "private pem", opener=open_app)
    assert client_id == "Iv1.test123456"
    assert requests[0][0].full_url == "https://api.github.com/app"
    assert requests[0][0].get_header("Authorization") == "Bearer trusted.jwt"
    assert "private pem" not in repr(requests)


@pytest.mark.parametrize("body", [
    '{"id": 124, "client_id": "Iv1.valid"}',
    '{"id": 123, "client_id": "abc\\nINJECTED=1"}',
    '{"id": 123}',
    "[]",
])
def test_invalid_metadata_fails_closed(body: str) -> None:
    with mock.patch.object(app_client, "build_app_jwt", return_value="trusted.jwt"):
        with pytest.raises(RuntimeError):
            app_client.resolve_client_id("123", "private pem", opener=lambda *_args, **_kw: Response(body))


def test_invalid_or_missing_credentials_rejected_before_network() -> None:
    with pytest.raises(ValueError):
        app_client.resolve_client_id("", "private pem")
    with pytest.raises(ValueError):
        app_client.resolve_client_id("123", "")


def test_main_writes_only_safe_client_id_to_actions_output(tmp_path, monkeypatch) -> None:
    output = tmp_path / "github-output"
    monkeypatch.setenv("FACTORY_APP_ID", "123")
    monkeypatch.setenv("FACTORY_APP_PRIVATE_KEY", "never-print-me")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(app_client, "resolve_client_id", lambda *_args: "Iv1.test123456")
    assert app_client.main() == 0
    assert output.read_text() == "client_id=Iv1.test123456\n"
