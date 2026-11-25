#!/usr/bin/env python3
"""Prove which catalog models draw on one exhausted prepaid credit balance.

A provider catalog row that publishes a positive price proves that the model
is billed against the credential's credit balance. An all-zero price proves
that it is not. A missing, negative or unreadable price proves nothing, so
that model stays eligible. Provider names, model IDs and name suffixes are
never evidence of billing membership.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CREDIT_POOL = "credit_balance"
PAID, FREE, UNKNOWN = "paid", "free", "unknown"
PRICE_FIELDS = ("prompt", "completion", "request", "input", "output")
APOSTROPHE = "['\u2019]"

# Only an attested empty balance proves that no other priced model can run.
# "Insufficient for this request" may still leave room for a cheaper model.
EMPTY_BALANCE_RE = re.compile(
    rf"(?:you{APOSTROPHE}re|you are|account is) out of credits|"
    r"account (?:credits|balance) (?:depleted|exhausted)",
    re.I,
)


def _price(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return price if price.is_finite() and price >= 0 else None


def billing_class(row: Mapping[str, Any]) -> str:
    """Return paid, free or unknown from one catalog row's published price."""
    pricing = row.get("pricing")
    if not isinstance(pricing, Mapping):
        return UNKNOWN
    tiers = pricing.get("tiers")
    blocks = [pricing, *(tiers if isinstance(tiers, list) else [])]
    prices: list[Decimal] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            return UNKNOWN
        for field in PRICE_FIELDS:
            if field in block:
                price = _price(block[field])
                if price is None:
                    return UNKNOWN
                prices.append(price)
    if not prices:
        return UNKNOWN
    return PAID if any(prices) else FREE


def membership(catalog: object) -> dict[str, str]:
    """Classify every model in an OpenAI-compatible catalog snapshot."""
    if isinstance(catalog, str):
        try:
            catalog = json.loads(catalog)
        except ValueError:
            return {}
    rows = catalog.get("data") if isinstance(catalog, Mapping) else None
    if not isinstance(rows, list):
        return {}
    return {
        row["id"]: billing_class(row)
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }


def credit_pool_members(catalog: object) -> frozenset[str]:
    """Models proven to draw on the credential's paid credit balance."""
    return frozenset(model for model, kind in membership(catalog).items() if kind == PAID)


def exhausted_pool(model: str, catalog: object, refusal: str) -> str | None:
    """Name the pool that this refusal proves empty, or None to cool one pair."""
    if not EMPTY_BALANCE_RE.search(refusal):
        return None
    return CREDIT_POOL if model in credit_pool_members(catalog) else None


def earliest_retry_at(observed: datetime) -> datetime:
    """When an empty credit pool is rechecked (factory_quota_reset)."""
    from factory_quota_reset import NEEDS_CREDITS_SECONDS

    return observed + timedelta(seconds=NEEDS_CREDITS_SECONDS)


def source_catalog(source: str, fleet_cache: Path) -> object:
    """The lease's catalog snapshot, or one read-only catalog fetch before it exists.

    The fetch never writes the fleet cache, which must stay a whole-fleet snapshot.
    """
    if fleet_cache.is_file():
        catalogs = json.loads(fleet_cache.read_text(encoding="utf-8"))
        if isinstance(catalogs, dict) and source in catalogs:
            return catalogs[source]
    from factory_published_model_catalog import PUBLIC_CATALOGS, published_catalog

    if source in PUBLIC_CATALOGS:
        return published_catalog(source)
    from factory_runtime_model_select import provider_catalog

    return provider_catalog(source, os.environ)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    trip = commands.add_parser("trip")
    trip.add_argument("--source", required=True)
    trip.add_argument("--model", required=True)
    trip.add_argument("--fleet-catalog-cache", type=Path, required=True)
    trip.add_argument("--log", type=Path, required=True)
    commands.add_parser("retry-at")
    args = parser.parse_args()
    if args.command == "retry-at":
        print(earliest_retry_at(datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return 0
    try:
        refusal = args.log.read_text(encoding="utf-8", errors="replace")
        catalog = source_catalog(args.source, args.fleet_catalog_cache)
    except (OSError, ValueError, RuntimeError):
        return 1
    pool = exhausted_pool(args.model, catalog, refusal)
    if pool is None:
        return 1
    print(pool)
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    sys.exit(main())
