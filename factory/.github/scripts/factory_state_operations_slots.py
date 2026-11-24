#!/usr/bin/env python3
"""Keep catalog slots off sources that have no model left to run.

A catalog slot has no pinned model, so model cooldowns never matched it and
the dispatcher admitted sources whose every catalog model was excluded. A slot
is eligible only when its source's live catalog lists at least one model
outside the trusted exclusions and any credit-pool cooldown. Sources without a
key-free published catalog, and any unreadable catalog, stay eligible: the run
job reads the authenticated catalog and fails the lease before a session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

CatalogReader = Callable[[str], Mapping[str, Any]]


def exhausted_sources(
    catalogs: Mapping[str, Mapping[str, Any]],
    exclusions: Mapping[str, list[str]],
) -> set[str]:
    """Sources whose catalog lists no model outside exclusions and credit pools."""
    from factory_billing_pool import credit_pool_members
    from factory_model_failover import _catalog_ids

    exhausted = set()
    for source, catalog in catalogs.items():
        try:
            models = set(_catalog_ids(source, None, catalog)[0])
        except Exception:  # noqa: BLE001 - an unreadable catalog never blocks dispatch
            continue
        models -= set(exclusions.get(source, ()))
        if source in exclusions.get("@credit_pool", ()):
            models -= credit_pool_members(catalog)
        if not models:
            exhausted.add(source)
    return exhausted


def drop_exhausted_slots(
    repo: str,
    workers: list[str],
    exclusions: Mapping[str, list[str]],
    reader: CatalogReader | None = None,
) -> tuple[list[str], list[str]]:
    from factory_published_model_catalog import PUBLIC_CATALOGS, published_catalog

    import factory_turso_operations as operations

    roster = operations.roster()
    wanted = {
        roster[w][0]
        for w in workers
        if w in roster and roster[w][1] in ("", "@catalog") and roster[w][0] in PUBLIC_CATALOGS
    }
    catalogs: dict[str, Mapping[str, Any]] = {}
    for source in sorted(wanted):
        try:
            catalogs[source] = (reader or published_catalog)(source)
        except Exception:  # noqa: BLE001 - an unreadable catalog never blocks dispatch
            continue
    exhausted = exhausted_sources(catalogs, exclusions)
    kept, rejected = [], []
    for worker in workers:
        source = roster.get(worker, ("", "x"))[0]
        if source in exhausted and roster[worker][1] in ("", "@catalog"):
            rejected.append(f"{worker}: every {source} catalog model is excluded or cooling")
        else:
            kept.append(worker)
    return kept, rejected
