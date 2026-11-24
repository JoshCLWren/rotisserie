"""Which of a provider's models draw on its paid credit balance, from the catalog alone.

The worker uses the same rule during a lease: after a provider says the account
is out of credits, it stops trying the models listed here as paid, and keeps
trying free and unknown ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from rotisserie.factory_console import runtime


class CatalogUnavailableError(RuntimeError):
    """No catalog for this provider could be read here."""


@dataclass(frozen=True)
class PoolMembership:
    provider: str
    origin: str
    paid: list[str] = field(default_factory=list)
    free: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "catalog": self.origin,
            "paid": self.paid,
            "free": self.free,
            "unknown": self.unknown,
        }


def read_catalog(provider: runtime.Provider, catalog_file: Path | None) -> tuple[object, str]:
    """A saved catalog (one provider's, or a worker's fleet snapshot) or the public one."""
    if catalog_file is not None:
        try:
            saved = json.loads(catalog_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogUnavailableError(f"cannot read {catalog_file}: {exc}") from exc
        if isinstance(saved, dict) and "data" not in saved and provider.source in saved:
            return saved[provider.source], f"{catalog_file} ({provider.source})"
        return saved, str(catalog_file)
    published = runtime.module("factory_published_model_catalog")
    if provider.source not in published.PUBLIC_CATALOGS:
        raise CatalogUnavailableError(
            f"{provider.key} has no public catalog; pass --catalog with a saved catalog file"
        )
    return published.published_catalog(provider.source, timeout=10), "public catalog"


def pool_membership(provider: runtime.Provider, catalog_file: Path | None = None) -> PoolMembership:
    catalog, origin = read_catalog(provider, catalog_file)
    kinds = runtime.module("factory_billing_pool").membership(catalog)
    grouped: dict[str, list[str]] = {"paid": [], "free": [], "unknown": []}
    for model, kind in sorted(kinds.items()):
        grouped[kind].append(model)
    return PoolMembership(provider.key, origin, **grouped)
