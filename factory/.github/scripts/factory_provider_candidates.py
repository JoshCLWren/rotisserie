#!/usr/bin/env python3
"""Normalize provider-specific model discovery into factory candidates.

The adapters in this module only interpret provider catalogs. Fetching a live
catalog remains the caller's responsibility so credentials, retry policy, and
provider-specific transport stay at the execution boundary.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from factory_provider_registry import PROVIDERS



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


# Catalogs such as NVIDIA's publish embedding, reranking, retrieval, reward
# and safety-classifier models beside chat models, with no type metadata.
# They can never run an OpenCode coding session; every lease that tried one
# (e.g. nvidia/embed-qa-4 in run 44091809870) burned a failover attempt and
# only earned a temporary cooldown. Without explicit "language" metadata, a
# model whose id names one of these non-chat capabilities is not a candidate.
_NON_CHAT_ID = re.compile(
    r"(?:^|[/_.-])(?:[a-z0-9]*embed[a-z0-9]*|rerank[a-z0-9]*|[a-z]*retriever|reward|"
    r"content-safety|safety-guard|[a-z]*guard|clip|nv-ingest|parse|detector|ocr)(?:$|[/_.:-])",
    re.I,
)


def _non_chat_model(model: str, item: dict[str, Any]) -> bool:
    """True for a model id naming a non-chat capability, absent type metadata."""
    return item.get("type") is None and bool(_NON_CHAT_ID.search(model))


class OpenAICompatibleAdapter:
    """Discover live provider models, not an approved price/ID roster."""

    mode = "catalog"

    def __init__(self, provider: str, runtime_prefix: str) -> None:
        self.provider = provider
        self.runtime_prefix = runtime_prefix

    def discover(
        self, raw_catalog: str, configured_models: Sequence[str] = (),
    ) -> Discovery:
        items = _json_catalog(raw_catalog)
        if items is None:
            return Discovery(self.provider, self.mode, "invalid", detail="invalid catalog JSON")

        configured = _configured_filter(configured_models)
        models: set[str] = set()
        for item in items:
            model = item.get("id")
            if not isinstance(model, str) or not model or any(ch.isspace() for ch in model):
                continue
            if configured is not None and model not in configured:
                continue
            # OpenRouter's free-account endpoint type, not a static model list.
            if self.provider == "openrouter-free" and not model.endswith(":free"):
                continue
            supported = item.get("supported_parameters")
            if isinstance(supported, list) and "tools" not in supported:
                continue
            if item.get("type") not in (None, "language"):
                continue
            if _non_chat_model(model, item):
                continue
            models.add(model)

        candidates = tuple(
            _candidate(self.provider, model, self.runtime_prefix, "provider_catalog")
            for model in sorted(models)
        )
        return Discovery(
            provider=self.provider, mode=self.mode,
            status="available" if candidates else "empty",
            candidates=candidates,
            detail=f"provider published {len(candidates)} candidate(s); execution not yet proven",
        )


class OpenCodeFreeAdapter:
    """Discover current OpenCode selectors without model-name or price rules."""

    provider = "opencode-free"
    mode = "cli_catalog"

    def discover(
        self, raw_catalog: str, configured_models: Sequence[str] = (),
    ) -> Discovery:
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
            provider=self.provider, mode=self.mode,
            status="available" if candidates else "empty",
            candidates=candidates,
            detail=f"OpenCode published {len(candidates)} candidate(s)",
        )


class RuntimeOnlyAdapter:
    """For a provider with no reliable, non-interactive enumeration contract."""

    mode = "runtime_only"

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def discover(
        self, raw_catalog: str, configured_models: Sequence[str] = (),
    ) -> Discovery:
        del raw_catalog, configured_models
        return Discovery(
            provider=self.provider, mode=self.mode, status="indeterminate",
            detail="requires an actual invocation; no enumerated candidates",
        )


# Credentials and published endpoints are integration metadata, not rosters.
# Any new compatible provider registered there automatically gains discovery.

ADAPTERS: dict[str, ProviderAdapter] = {
    source: OpenAICompatibleAdapter(source, spec.provider)
    for source, spec in PROVIDERS.items()
}
ADAPTERS.update({
    "opencode-free": OpenCodeFreeAdapter(),
    "openrouter-free": OpenAICompatibleAdapter("openrouter-free", "openrouter"),
    "omniroute-free": OpenAICompatibleAdapter("omniroute-free", "omniroute"),
    "kilo-auto": RuntimeOnlyAdapter("kilo-auto"),
})


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
