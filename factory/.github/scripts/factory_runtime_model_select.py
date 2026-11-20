#!/usr/bin/env python3
"""Discover an executable candidate from the selected provider at run time.

Catalog records are hints, not hardcoded model permissions. Published prices
are never an eligibility condition; actual invocation proves callability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from factory_provider_candidates import ADAPTERS, discover
from factory_provider_registry import PROVIDERS, missing_credentials


class CandidateUnavailableError(RuntimeError):
    """No published model can currently be selected for an invocation."""


def catalog_url(source: str, spec: Any, env: Mapping[str, str]) -> str:
    """Resolve the provider's published catalog endpoint, never a model ID."""
    endpoint = spec.endpoint
    if spec.additional_credentials:
        endpoint = endpoint.replace("{account_id}", env["CLOUDFLARE_ACCOUNT_ID"])
    if source == "pollinations":
        # Provider-side filters guarantee that discovery contains only usable
        # text/chat models with tool calling, excluding managed agents.
        return endpoint.rstrip("/") + "/models?category=text&capabilities=tool_calling&agent=false"
    if source == "cloudflare-workers-ai":
        # Cloudflare publishes Workers AI models at models/search, not at
        # its OpenAI-compatible inference /ai/v1/models path.
        return (
            endpoint.removesuffix("/ai/v1")
            + "/ai/models/search?format=openrouter&per_page=100"
        )
    return endpoint.rstrip("/") + "/models"


def provider_catalog(
    source: str, env: Mapping[str, str], *, timeout: float = 20.0,
) -> dict[str, Any]:
    spec = PROVIDERS.get(source)
    if spec is None:
        raise CandidateUnavailableError(f"no compatible provider integration for {source}")
    missing = missing_credentials(spec, env)
    if missing:
        raise CandidateUnavailableError(f"missing provider credentials for {source}")
    url = catalog_url(source, spec, env)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {env[spec.credential]}",
            "User-Agent": "ComicPile-runtime-catalog/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise CandidateUnavailableError(f"{source}: catalog HTTP {response.status}")
            body = response.read(5_000_001)
    except (OSError, urllib.error.URLError) as exc:
        raise CandidateUnavailableError(f"{source}: catalog read failed") from exc
    if len(body) > 5_000_000:
        raise CandidateUnavailableError(f"{source}: catalog is too large")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CandidateUnavailableError(f"{source}: invalid catalog JSON") from exc
    if not isinstance(value, dict):
        raise CandidateUnavailableError(f"{source}: catalog must be an object")
    return value


def select(
    source: str, worker: str, catalog: Mapping[str, Any],
    exclusions: Mapping[str, list[str]] | None = None,
) -> dict[str, str | bool]:
    """Rank published candidates without a checked-in model inventory."""
    result = discover(source, json.dumps(catalog))
    excluded = set((exclusions or {}).get(source, []))
    candidates = [
        candidate for candidate in result.candidates
        if candidate.model not in excluded
    ]
    if not candidates:
        raise CandidateUnavailableError(
            f"{source}: catalog contained no eligible candidates outside cooldown"
        )
    digest = hashlib.sha256(f"{source}:{worker}".encode()).digest()
    candidate = candidates[int.from_bytes(digest[:8], "big") % len(candidates)]
    return {
        "source": source,
        "model": candidate.model,
        "runtime_model": candidate.runtime_model,
        "display": f"{source} · {candidate.model}",
        "branch_suffix": source,
        "reason": "selected-from-live-provider-catalog",
        "catalog_listed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, choices=sorted(ADAPTERS))
    parser.add_argument("--worker", required=True)
    parser.add_argument("--exclusions-file", type=Path)
    parser.add_argument("--catalog-json", type=Path, help="synthetic offline test catalog")
    args = parser.parse_args()
    try:
        if not args.worker.isdecimal():
            raise CandidateUnavailableError("invalid worker identity")
        raw = (
            json.loads(args.catalog_json.read_text(encoding="utf-8"))
            if args.catalog_json else provider_catalog(args.source, os.environ)
        )
        if not isinstance(raw, dict):
            raise CandidateUnavailableError("catalog is not a JSON object")
        excluded: Mapping[str, list[str]] | None = None
        if args.exclusions_file is not None:
            value = json.loads(args.exclusions_file.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or any(
                not isinstance(items, list)
                or not all(isinstance(item, str) for item in items)
                for items in value.values()
            ):
                raise CandidateUnavailableError("invalid trusted model cooldown projection")
            excluded = value
        result = select(args.source, args.worker, raw, excluded)
    except (CandidateUnavailableError, OSError, ValueError) as exc:
        print(f"factory catalog: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
