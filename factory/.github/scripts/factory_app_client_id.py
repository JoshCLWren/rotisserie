#!/usr/bin/env python3
"""Resolve a preexisting GitHub App's client ID without changing its credentials.

GitHub deprecated the numeric app-id input for create-github-app-token@v3.
Query the authoritative /app endpoint using a short-lived, locally signed JWT.
The private key never appears in outputs, remote URLs, logs, or artifacts.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from factory_worker_github_app import build_app_jwt


def resolve_client_id(
    app_id: str,
    private_key: str,
    *,
    opener: Any = None,
) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", app_id) or not private_key.strip():
        raise ValueError("configured GitHub App ID and private key are required")
    jwt = build_app_jwt(app_id, private_key)
    request = urllib.request.Request(
        "https://api.github.com/app",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "comic-pile-factory-app-client-id",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=30) as response:
            app = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError("GitHub App metadata request failed") from exc
    if not isinstance(app, dict) or app.get("id") != int(app_id):
        raise RuntimeError("GitHub App metadata did not match the requested App")
    client_id = app.get("client_id")
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{5,128}", client_id):
        raise RuntimeError("GitHub App metadata returned an invalid client ID")
    return client_id


def main() -> int:
    try:
        client_id = resolve_client_id(
            os.environ.get("FACTORY_APP_ID", ""),
            os.environ.get("FACTORY_APP_PRIVATE_KEY", ""),
        )
        output = os.environ["GITHUB_OUTPUT"]
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"client_id={client_id}\n")
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"[factory-app] cannot resolve client ID: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
