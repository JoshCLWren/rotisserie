#!/usr/bin/env python3
"""Existing OpenCode factory lane, authenticated exclusively to the Go workspace."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SOURCE = "opencode-free"  # Keep existing worker IDs, claims and Neon history.
PROVIDER = "opencode-go"
PREFERRED = "hy4-flash-free"
CATALOG_URL = "https://opencode.ai/console/api/v1/models"


class GoCatalogError(ValueError):
    """Go-account access or free capacity was not established."""


def catalog(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Require a workspace model list that explicitly contains OpenCode Go."""
    key = (os.environ if env is None else env).get("OPENCODE_API_KEY", "")
    if not key:
        raise GoCatalogError("Go account key missing")
    request = urllib.request.Request(
        CATALOG_URL, headers={
            "Authorization": f"Bearer {key}", "Accept": "application/json",
            "User-Agent": "Rotisserie-factory-Go/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise GoCatalogError("Go workspace model list unauthorized")
            body = response.read(5_000_001)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise GoCatalogError("Go workspace model list request failed") from exc
    if len(body) > 5_000_000:
        raise GoCatalogError("Go catalog too large")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise GoCatalogError("Go catalog invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get(PROVIDER), dict):
        raise GoCatalogError("Credential does not expose a Go subscription")
    return payload


def _zero(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, ValueError):
        return False


def free_models(payload: Mapping[str, Any]) -> list[str]:
    provider = payload.get(PROVIDER)
    models = provider.get("models") if isinstance(provider, dict) else None
    if not isinstance(models, dict):
        return []
    eligible: list[str] = []
    for model_id, model in models.items():
        if not isinstance(model_id, str) or not model_id or not isinstance(model, dict):
            continue
        capabilities = model.get("capabilities")
        tools = capabilities.get("tools") if isinstance(capabilities, dict) else None
        if model.get("type") != "chat" or not isinstance(tools, dict) or tools.get("supported") is not True:
            continue
        costs = model.get("cost")
        text = costs.get("text") if isinstance(costs, dict) else None
        inp = text.get("input") if isinstance(text, dict) else None
        out = text.get("output") if isinstance(text, dict) else None
        if not isinstance(inp, dict) or not isinstance(out, dict):
            continue
        cache = inp.get("cache", {})
        if (
            _zero(inp.get("price")) and _zero(out.get("price"))
            and isinstance(cache, dict)
            and all(_zero(value) for value in cache.values())
        ):
            eligible.append(model_id)
    return sorted(eligible)


def choose(payload: Mapping[str, Any], worker: str, rotation: int = 0, excluded: set[str] | None = None) -> dict[str, str]:
    available = [model for model in free_models(payload) if model not in (excluded or set())]
    if not available:
        raise GoCatalogError("No zero-cost Go coding models outside cooldown")
    if not worker.isdecimal() or int(worker) < 1:
        raise GoCatalogError("Invalid worker")
    model = PREFERRED if PREFERRED in available else available[(int(worker) + rotation) % len(available)]
    return {"source": SOURCE, "provider": PROVIDER, "model": model,
            "runtime_model": f"{PROVIDER}/{model}", "reason": "verified-Go-free-model"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    parser.add_argument("--rotation", type=int, default=0)
    parser.add_argument("--exclusions-file", type=Path)
    parser.add_argument("--catalog-json", type=Path)
    parser.add_argument("--catalog-output", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.catalog_json.read_text()) if args.catalog_json else catalog()
        if not isinstance(payload, dict) or not isinstance(payload.get(PROVIDER), dict):
            raise GoCatalogError("Go workspace provider missing")
        state = json.loads(args.exclusions_file.read_text()) if args.exclusions_file else {}
        values = state.get(SOURCE, []) if isinstance(state, dict) else None
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise GoCatalogError("Invalid model cooldown snapshot")
        selected = choose(payload, args.worker, args.rotation, set(values))
        if args.catalog_output:
            args.catalog_output.write_text(json.dumps(payload), encoding="utf-8")
            args.catalog_output.chmod(0o600)
        print(json.dumps(selected, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"OpenCode Go unavailable: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
