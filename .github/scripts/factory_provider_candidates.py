#!/usr/bin/env python3
"""Normalize provider-specific model discovery into factory candidates.

The adapters in this module only interpret provider catalogs. Fetching a live
catalog remains the caller's responsibility so credentials, retry policy, and
provider-specific transport stay at the execution boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Candidate:
    """One provider/model pair eligible for a runtime execution attempt."""

    provider: str
    model: str
    runtime_model: str
    discovered_by: str


@dataclass(frozen=True)
class Discovery:
    """Normalized result from one provider adapter."""

    provider: str
    mode: str
    status: str
    candidates: tuple[Candidate, ...] = ()
    detail: str = ""


class ProviderAdapter(Protocol):
    """Contract implemented by each supported provider."""

    provider: str
    mode: str

    def discover(
        self,
        raw_catalog: str,
        configured_models: Sequence[str] = (),
    ) -> Discovery:
        """Return candidates proven by the supplied provider catalog."""


def _configured_filter(models: Sequence[str]) -> set[str] | None:
    """Return an optional policy filter for configured model identifiers."""
    configured = {model.strip() for model in models if model.strip()}
    return configured or None


def _candidate(
    provider: str,
    model: str,
    runtime_prefix: str,
    discovered_by: str,
) -> Candidate:
    """Build normalized runtime metadata for a discovered model."""
    return Candidate(
        provider=provider,
        model=model,
        runtime_model=f"{runtime_prefix}/{model}",
        discovered_by=discovered_by,
    )


def _json_catalog(raw_catalog: str) -> list[dict[str, Any]] | None:
    """Parse an OpenAI-compatible model catalog, failing closed."""
    try:
        payload = json.loads(raw_catalog)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("data")
    if not isinstance(items, list) or not all(
        isinstance(item, dict) for item in items
    ):
        return None
    return [item for item in items if isinstance(item, dict)]


class OpenAICompatibleAdapter:
    """Adapter for providers exposing an OpenAI-compatible model catalog."""

    def __init__(
        self,
        provider: str,
        runtime_prefix: str,
        *,
        free_only: bool = False,
    ) -> None:
        """Configure provider metadata and eligibility policy."""
        self.provider = provider
        self.runtime_prefix = runtime_prefix
        self.free_only = free_only
        self.mode = "catalog"

    def discover(
        self,
        raw_catalog: str,
        configured_models: Sequence[str] = (),
    ) -> Discovery:
        """Return catalog models allowed by free-cost and configured policy."""
        items = _json_catalog(raw_catalog)
        if items is None:
            return Discovery(
                provider=self.provider,
                mode=self.mode,
                status="invalid",
                detail="provider catalog was not valid OpenAI-compatible JSON",
            )

        configured = _configured_filter(configured_models)
        models: set[str] = set()
        for item in items:
            model = item.get("id")
            if not isinstance(model, str) or not model:
                continue
            if configured is not None and model not in configured:
                continue
            if self.free_only and not _openrouter_model_is_free(item):
                continue
            models.add(model)

        candidates = tuple(
            _candidate(self.provider, model, self.runtime_prefix, "provider_catalog")
            for model in sorted(models)
        )
        return Discovery(
            provider=self.provider,
            mode=self.mode,
            status="available" if candidates else "empty",
            candidates=candidates,
            detail=f"provider catalog exposed {len(candidates)} eligible candidate(s)",
        )


class OpenCodeFreeAdapter:
    """Adapter for the project-scoped output of opencode models."""

    provider = "opencode-free"
    mode = "cli_catalog"

    def discover(
        self,
        raw_catalog: str,
        configured_models: Sequence[str] = (),
    ) -> Discovery:
        """Parse available OpenCode provider/model selectors."""
        configured = _configured_filter(configured_models)
        models: set[str] = set()
        for raw_line in raw_catalog.splitlines():
            selector = raw_line.strip()
            if not selector or selector.startswith("#") or "/" not in selector:
                continue
            provider_id, model = selector.split("/", 1)
            if provider_id != "opencode" or not model:
                continue
            if configured is not None and model not in configured:
                continue
            models.add(model)

        candidates = tuple(
            _candidate(self.provider, model, "opencode", "opencode_models")
            for model in sorted(models)
        )
        return Discovery(
            provider=self.provider,
            mode=self.mode,
            status="available" if candidates else "empty",
            candidates=candidates,
            detail=f"OpenCode exposed {len(candidates)} eligible candidate(s)",
        )


class OmniRouteFreeAdapter(OpenAICompatibleAdapter):
    """Expose OmniRoute's native free intent routes for factory execution."""

    FACTORY_ROUTES = ("auto/coding:free", "auto/reasoning:free")

    def __init__(self) -> None:
        """Configure the external OmniRoute OpenAI-compatible adapter."""
        super().__init__("omniroute-free", "omniroute")

    def discover(
        self,
        raw_catalog: str,
        configured_models: Sequence[str] = (),
    ) -> Discovery:
        """Expose native virtual intents while OmniRoute owns model selection."""
        items = _json_catalog(raw_catalog)
        if items is None:
            return Discovery(
                provider=self.provider,
                mode=self.mode,
                status="invalid",
                detail="provider catalog was not valid OpenAI-compatible JSON",
            )

        configured = _configured_filter(configured_models)
        routes = tuple(
            route
            for route in self.FACTORY_ROUTES
            if configured is None or route in configured
        )
        if not routes:
            return Discovery(
                provider=self.provider,
                mode=self.mode,
                status="empty",
                detail="configured policy excluded the native OmniRoute factory routes",
            )

        catalog_models = {
            str(item.get("id"))
            for item in items
            if isinstance(item.get("id"), str)
        }
        candidates = tuple(
            _candidate(
                self.provider,
                route,
                self.runtime_prefix,
                (
                    "provider_catalog"
                    if route in catalog_models
                    else "native_auto_route_fallback"
                ),
            )
            for route in routes
        )
        return Discovery(
            provider=self.provider,
            mode=self.mode,
            status="available",
            candidates=candidates,
            detail=(
                "OmniRoute native auto/coding:free and auto/reasoning:free "
                "intents delegate free-tier, tool-compatibility, quality, "
                "quota, and fallback decisions to OmniRoute"
            ),
        )


class RuntimeOnlyAdapter:
    """Adapter for a provider route that cannot be enumerated reliably."""

    def __init__(self, provider: str) -> None:
        """Configure provider metadata and eligibility policy."""
        self.provider = provider
        self.mode = "runtime_only"

    def discover(
        self,
        raw_catalog: str,
        configured_models: Sequence[str] = (),
    ) -> Discovery:
        """Fail closed until an actual execution or trusted probe proves a route."""
        del raw_catalog, configured_models
        return Discovery(
            provider=self.provider,
            mode=self.mode,
            status="indeterminate",
            detail=(
                "provider cannot enumerate candidates reliably; "
                "use normalized runtime evidence"
            ),
        )


def _decimal_is_zero(value: Any) -> bool:
    """Return whether a provider price value is numeric zero."""
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _openrouter_model_is_free(item: dict[str, Any]) -> bool:
    """Return whether OpenRouter catalog metadata proves a free model."""
    model = item.get("id")
    if isinstance(model, str) and (
        model.endswith(":free") or model == "openrouter/free"
    ):
        return True

    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        return False
    return _decimal_is_zero(pricing.get("prompt")) and _decimal_is_zero(
        pricing.get("completion")
    )


ADAPTERS: dict[str, ProviderAdapter] = {
    # NVIDIA documents model invocation but not a reliable account-scoped
    # enumeration contract. Actual probes remain the authoritative evidence.
    "nvidia": RuntimeOnlyAdapter("nvidia"),
    "opencode-free": OpenCodeFreeAdapter(),
    "openrouter-free": OpenAICompatibleAdapter(
        "openrouter-free",
        "openrouter",
        free_only=True,
    ),
    # OmniRoute owns discovery, free-tier filtering, quality scoring, tool
    # compatibility, quota handling, and fallback across underlying providers.
    "omniroute-free": OmniRouteFreeAdapter(),
    # Kilo documents a changing free model set exposed through interactive model
    # selection, but no reliable non-interactive enumeration contract. Keep that
    # limitation here and require actual attempt evidence before counting it.
    "kilo-auto": RuntimeOnlyAdapter("kilo-auto"),
    # Z.AI and Ollama Cloud are fixed free OpenAI-compatible pins. They are not
    # enumerated by OpenCode CLI catalogs; runtime smoke is the evidence.
    "z-ai": RuntimeOnlyAdapter("z-ai"),
    "ollama-cloud": RuntimeOnlyAdapter("ollama-cloud"),
}


def discover(
    provider: str,
    raw_catalog: str,
    configured_models: Sequence[str] = (),
) -> Discovery:
    """Discover normalized candidates for one supported provider.

    Raises:
        ValueError: If the provider has no registered adapter.

    """
    adapter = ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(f"unsupported factory provider: {provider}")
    return adapter.discover(raw_catalog, configured_models)


def main() -> int:
    """Run one adapter and print its normalized JSON result."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True, choices=sorted(ADAPTERS))
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--configured-model", action="append", default=[])
    args = parser.parse_args()

    raw_catalog = ""
    if args.catalog is not None:
        raw_catalog = args.catalog.read_text(encoding="utf-8", errors="replace")
    result = discover(args.provider, raw_catalog, args.configured_model)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
