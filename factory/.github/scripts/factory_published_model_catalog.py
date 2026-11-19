#!/usr/bin/env python3
"""Discover model candidates from provider-published catalogs.

Published catalog presence is not proof of successful execution.
Published pricing does not gate other providers. The openrouter-free lane
selects only OpenRouter's provider-designated :free endpoints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# These provider endpoints are integration metadata, not a model inventory.
PUBLIC_CATALOGS: dict[str, str] = {
    "openrouter-free": "https://openrouter.ai/api/v1/models",
    "vercel-ai-gateway": "https://ai-gateway.vercel.sh/v1/models",
}


class PublishedCatalogUnavailableError(RuntimeError):
    """A provider catalog cannot supply a usable candidate."""



def _executable_candidate(source: str, item: Mapping[str, Any]) -> bool:
    """Catalog presence creates an execution candidate, not proven callability.

    Known incompatible modalities are excluded. Missing optional capability
    metadata and published prices never block a real execution attempt.
    """
    model = item.get("id")
    if not isinstance(model, str) or not model or any(ch.isspace() for ch in model):
        return False
    # OpenRouter identifies no-credit endpoints with :free.
    # A zero-priced regular endpoint is not necessarily free-key callable.
    # This is a provider endpoint contract, not a curated model roster.
    if source == "openrouter-free" and not model.endswith(":free"):
        return False
    supported = item.get("supported_parameters")
    if isinstance(supported, list) and "tools" not in supported:
        return False
    if source == "vercel-ai-gateway" and item.get("type") not in (None, "language"):
        return False
    endpoints = item.get("endpoints")
    if (source == "openrouter-free" and isinstance(endpoints, list) and endpoints
            and "/api/v1/chat/completions" not in endpoints):
        return False
    return True


def catalog_model_ids(source: str, catalog: Mapping[str, Any]) -> list[str]:
    """Return live IDs; OpenRouter's free lane uses only :free endpoints."""
    if source not in PUBLIC_CATALOGS:
        raise ValueError(f"source has no published model catalog: {source}")
    rows = catalog.get("data")
    if not isinstance(rows, list):
        raise PublishedCatalogUnavailableError("published model catalog has no data list")
    return sorted({
        row["id"] for row in rows
        if isinstance(row, Mapping) and _executable_candidate(source, row)
    })


def choose_model(
    source: str, worker: str, catalog: Mapping[str, Any],
    exclusions: set[str] | None = None,
) -> str:
    if not worker.isdecimal():
        raise ValueError("worker must be a numeric slot ID")
    candidates = [
        model for model in catalog_model_ids(source, catalog)
        if model not in (exclusions or set())
    ]
    if not candidates:
        raise PublishedCatalogUnavailableError(f"{source}: no eligible catalog candidates available")
    # Deterministic across runs, independent of manually curated roster pins.
    seed = hashlib.sha256(f"{source}:{worker}".encode()).digest()
    return candidates[int.from_bytes(seed[:8], "big") % len(candidates)]


def published_catalog(source: str, *, timeout: float = 20) -> dict[str, Any]:
    url = PUBLIC_CATALOGS.get(source)
    if url is None:
        raise ValueError(f"no published catalog contract for {source}")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "ComicPile-factory-catalog/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise PublishedCatalogUnavailableError(f"{source}: model catalog HTTP {response.status}")
            raw = response.read(5_000_001)
    except (OSError, urllib.error.URLError) as exc:
        raise PublishedCatalogUnavailableError(f"{source}: model catalog unavailable") from exc
    if len(raw) > 5_000_000:
        raise PublishedCatalogUnavailableError(f"{source}: model catalog exceeded size limit")
    try:
        catalog = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PublishedCatalogUnavailableError(f"{source}: invalid published catalog JSON") from exc
    if not isinstance(catalog, dict):
        raise PublishedCatalogUnavailableError(f"{source}: catalog must be a JSON object")
    return catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=sorted(PUBLIC_CATALOGS))
    parser.add_argument("--worker", required=True)
    parser.add_argument("--catalog-json", type=Path, help="offline fixture for tests only")
    parser.add_argument("--exclusions-file", type=Path)
    args = parser.parse_args(argv)
    try:
        catalog = (
            json.loads(args.catalog_json.read_text(encoding="utf-8"))
            if args.catalog_json else published_catalog(args.source)
        )
        if not isinstance(catalog, dict):
            raise PublishedCatalogUnavailableError("catalog must be an object")
        excluded: set[str] = set()
        if args.exclusions_file and args.exclusions_file.is_file():
            raw = json.loads(args.exclusions_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise PublishedCatalogUnavailableError("Neon model exclusions are invalid")
            models = raw.get(args.source, [])
            if not isinstance(models, list) or not all(isinstance(x, str) for x in models):
                raise PublishedCatalogUnavailableError("Neon model exclusions are invalid")
            excluded = set(models)
        model = choose_model(args.source, args.worker, catalog, excluded)
    except (PublishedCatalogUnavailableError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"factory catalog: {exc}", file=sys.stderr)
        return 1
    from factory_provider_registry import PROVIDERS

    spec = PROVIDERS.get(args.source)
    runtime_provider = spec.provider if spec else args.source.removesuffix("-free")
    print(json.dumps({
        "source": args.source, "model": model,
        "runtime_model": f"{runtime_provider}/{model}",
        "catalog_listed": True,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
