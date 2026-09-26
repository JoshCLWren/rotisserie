#!/usr/bin/env python3
"""Load factory-relevant OpenCode CLI catalogs.

Source of truth is ``opencode models <provider>`` (plain or ``--verbose``),
matching ``scripts/opencode-model-scout.sh`` and the factory runner. NVIDIA
presence comes from the OpenCode nvidia list, never integrate.api.nvidia.com.

CI and unit tests inject a recorded catalog JSON fixture when the ``opencode``
binary is unavailable. The scheduled path calls the real CLI when present.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

FACTORY_PROVIDERS = ("opencode", "nvidia", "openrouter")
CATALOG_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "opencode-model-catalog.sh"


class CatalogUnavailableError(RuntimeError):
    """Raised when a required provider catalog cannot be loaded."""


@dataclass(frozen=True)
class CatalogEntry:
    """One model exposed by an OpenCode provider catalog."""

    provider: str
    model_id: str
    cost_input: Decimal | None = None
    cost_output: Decimal | None = None

    @property
    def cost_is_zero(self) -> bool | None:
        """Return True/False when both cost sides are known, else None."""
        if self.cost_input is None or self.cost_output is None:
            return None
        return self.cost_input == 0 and self.cost_output == 0

    @property
    def cost_is_paid(self) -> bool:
        """Return True only when verbose cost proves a non-zero price."""
        if self.cost_input is None or self.cost_output is None:
            return False
        return self.cost_input > 0 or self.cost_output > 0


@dataclass(frozen=True)
class ProviderCatalog:
    """Normalized models for one OpenCode provider id."""

    provider: str
    entries: tuple[CatalogEntry, ...]
    source: str

    def by_id(self) -> dict[str, CatalogEntry]:
        """Index entries by bare model id."""
        return {entry.model_id: entry for entry in self.entries}

    def model_ids(self) -> set[str]:
        """Return bare model ids in this catalog."""
        return set(self.by_id())

    def get(self, model_id: str) -> CatalogEntry | None:
        """Return the catalog row for a roster or selector model id."""
        bare = strip_provider_prefix(model_id, self.provider)
        return self.by_id().get(bare)


def strip_provider_prefix(model_id: str, provider: str) -> str:
    """Remove a leading ``provider/`` prefix when present.

    Args:
        model_id: Selector or bare model id.
        provider: OpenCode provider id.

    Returns:
        Model id relative to that provider.
    """
    raw = model_id.strip()
    prefix = f"{provider}/"
    if raw.startswith(prefix):
        return raw[len(prefix) :]
    return raw


def _decimal(value: object) -> Decimal | None:
    """Parse a catalog cost, failing closed on non-numeric values."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _costs_from_metadata(metadata: Mapping[str, object]) -> tuple[Decimal | None, Decimal | None]:
    """Extract input/output cost from verbose OpenCode metadata."""
    pricing: object = metadata.get("pricing")
    if pricing is None:
        pricing = metadata.get("cost")
    if isinstance(pricing, list) and pricing:
        first = pricing[0]
        pricing = first if isinstance(first, Mapping) else None
    if not isinstance(pricing, Mapping):
        return None, None
    inbound = pricing.get("input")
    if inbound is None:
        inbound = pricing.get("prompt")
    outbound = pricing.get("output")
    if outbound is None:
        outbound = pricing.get("completion")
    return _decimal(inbound), _decimal(outbound)


def _entry_from_mapping(provider: str, item: Mapping[str, object]) -> CatalogEntry | None:
    """Build one catalog entry from fixture or verbose JSON metadata."""
    raw_id = item.get("id") or item.get("model") or item.get("modelID") or item.get("model_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    model_id = strip_provider_prefix(raw_id, provider)
    if not model_id:
        return None
    cost_block = item.get("cost")
    if isinstance(cost_block, Mapping) and (
        "input" in cost_block or "output" in cost_block or "prompt" in cost_block
    ):
        inbound, outbound = _costs_from_metadata({"cost": cost_block})
    else:
        inbound, outbound = _costs_from_metadata(item)
        if inbound is None:
            inbound = _decimal(item.get("cost_input"))
        if outbound is None:
            outbound = _decimal(item.get("cost_output"))
    return CatalogEntry(
        provider=provider,
        model_id=model_id,
        cost_input=inbound,
        cost_output=outbound,
    )


def parse_opencode_models_text(text: str, provider: str, *, source: str = "cli") -> ProviderCatalog:
    """Parse ``opencode models <provider>`` plain or verbose text.

    Args:
        text: CLI stdout.
        provider: Provider filter that produced the text.
        source: Provenance label (``cli`` or ``fixture``).

    Returns:
        Normalized provider catalog. Empty text is an empty catalog, not invalid.

    Raises:
        CatalogUnavailableError: When the payload claims to be JSON but is not a
            catalog the adapter understands.
    """
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            payload: object = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise CatalogUnavailableError(
                f"{provider} catalog JSON was malformed: {exc}"
            ) from exc
        return parse_catalog_payload(payload, provider=provider, source=source)

    entries: dict[str, CatalogEntry] = {}
    pending: str | None = None
    depth = 0
    chunks: list[str] = []

    def flush_json() -> None:
        nonlocal pending
        if not chunks or pending is None:
            chunks.clear()
            return
        try:
            metadata = json.loads("".join(chunks))
        except json.JSONDecodeError:
            chunks.clear()
            return
        chunks.clear()
        if not isinstance(metadata, dict):
            return
        inbound, outbound = _costs_from_metadata(metadata)
        previous = entries.get(pending)
        entries[pending] = CatalogEntry(
            provider=provider,
            model_id=pending,
            cost_input=inbound if inbound is not None else (
                previous.cost_input if previous is not None else None
            ),
            cost_output=outbound if outbound is not None else (
                previous.cost_output if previous is not None else None
            ),
        )
        pending = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if depth or line.startswith("{"):
            chunks.append(raw_line)
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                depth = 0
                flush_json()
            continue
        selector = line.split()[0]
        model_id = strip_provider_prefix(selector, provider)
        if not model_id or model_id == selector and "/" in selector:
            # Reject selectors from a different provider.
            if "/" in selector and not selector.startswith(f"{provider}/"):
                pending = None
                continue
        if not model_id:
            continue
        pending = model_id
        entries.setdefault(
            model_id,
            CatalogEntry(provider=provider, model_id=model_id),
        )

    return ProviderCatalog(
        provider=provider,
        entries=tuple(sorted(entries.values(), key=lambda item: item.model_id)),
        source=source,
    )


def parse_catalog_payload(
    payload: object,
    *,
    provider: str,
    source: str = "fixture",
) -> ProviderCatalog:
    """Parse one provider's fixture or OpenAI-style catalog JSON."""
    items: list[object]
    if isinstance(payload, Mapping):
        raw = payload.get("data")
        if raw is None:
            raw = payload.get("models")
        if raw is None:
            raw = payload.get("entries")
        if isinstance(raw, list):
            items = raw
        elif "id" in payload or "model" in payload:
            items = [payload]
        else:
            items = []
    elif isinstance(payload, list):
        items = payload
    else:
        raise CatalogUnavailableError(
            f"{provider} catalog payload was not a JSON object or array"
        )

    entries: dict[str, CatalogEntry] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        entry = _entry_from_mapping(provider, item)
        if entry is None:
            continue
        entries[entry.model_id] = entry
    return ProviderCatalog(
        provider=provider,
        entries=tuple(sorted(entries.values(), key=lambda item: item.model_id)),
        source=source,
    )


def parse_catalog_fixture(payload: Mapping[str, object]) -> dict[str, ProviderCatalog]:
    """Parse a recorded multi-provider catalog fixture.

    Args:
        payload: JSON object with a ``providers`` map or per-provider keys.

    Returns:
        Provider id to catalog.

    Raises:
        CatalogUnavailableError: When the fixture is not an object map.
    """
    raw_providers = payload.get("providers")
    if raw_providers is None:
        raw_providers = {key: payload[key] for key in FACTORY_PROVIDERS if key in payload}
    if not isinstance(raw_providers, Mapping):
        raise CatalogUnavailableError("catalog fixture missing providers object")

    catalogs: dict[str, ProviderCatalog] = {}
    for provider, raw in raw_providers.items():
        name = str(provider).strip()
        if name not in FACTORY_PROVIDERS:
            continue
        if isinstance(raw, str):
            catalogs[name] = parse_opencode_models_text(raw, name, source="fixture")
            continue
        catalogs[name] = parse_catalog_payload(raw, provider=name, source="fixture")
    return catalogs


def load_catalog_fixture(path: Path) -> dict[str, ProviderCatalog]:
    """Load catalogs from a recorded JSON fixture file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogUnavailableError(f"catalog fixture was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogUnavailableError("catalog fixture root must be a JSON object")
    return parse_catalog_fixture(payload)


def fetch_cli_catalog(
    provider: str,
    *,
    opencode_bin: str | None = None,
    helper: Path | None = None,
    refresh: bool = True,
    verbose: bool = True,
    timeout: float = 60,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run OpenCode CLI (or the repo catalog helper) for one provider.

    Args:
        provider: ``opencode``, ``nvidia``, or ``openrouter``.
        opencode_bin: Optional ``opencode`` executable. Defaults to ``PATH``.
        helper: Optional ``scripts/opencode-model-catalog.sh``.
        refresh: Pass ``--refresh`` so models.dev cache is updated.
        verbose: Pass ``--verbose`` so cost metadata is available.
        timeout: Subprocess timeout in seconds.
        env: Optional environment overlay.

    Returns:
        Raw CLI stdout.

    Raises:
        CatalogUnavailableError: When the binary is missing or the command fails.
    """
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    catalog_helper = helper if helper is not None else CATALOG_HELPER
    if catalog_helper.is_file() and os.access(catalog_helper, os.X_OK):
        command = [
            str(catalog_helper),
            provider,
            "verbose" if verbose else "plain",
            "refresh" if refresh else "no-refresh",
        ]
        if opencode_bin:
            command_env["OPENCODE_BIN"] = opencode_bin
    else:
        binary = opencode_bin or shutil.which("opencode")
        if not binary:
            raise CatalogUnavailableError(
                "opencode binary is unavailable; inject --catalog-json in CI"
            )
        command = [binary, "models", provider]
        if refresh:
            command.append("--refresh")
        if verbose:
            command.append("--verbose")
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=command_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CatalogUnavailableError(f"opencode models {provider} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise CatalogUnavailableError(f"opencode models {provider} failed: {detail}")
    return result.stdout


def load_provider_catalogs(
    *,
    fixture_path: Path | None = None,
    text_paths: Mapping[str, Path] | None = None,
    providers: Sequence[str] = FACTORY_PROVIDERS,
    require_cli: bool = False,
    opencode_bin: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, ProviderCatalog]:
    """Load catalogs from a fixture, recorded text dumps, or the live CLI.

    Args:
        fixture_path: Recorded multi-provider JSON fixture.
        text_paths: Optional per-provider CLI stdout files.
        providers: Providers that must be present when ``require_cli`` is set.
        require_cli: Call the real CLI for any provider still missing.
        opencode_bin: Optional ``opencode`` executable override.
        env: Optional environment overlay for the CLI.

    Returns:
        Provider catalogs keyed by OpenCode provider id.

    Raises:
        CatalogUnavailableError: When a required live catalog cannot be fetched.
    """
    catalogs: dict[str, ProviderCatalog] = {}
    if fixture_path is not None:
        catalogs.update(load_catalog_fixture(fixture_path))
    if text_paths:
        for provider, path in text_paths.items():
            if provider not in FACTORY_PROVIDERS:
                continue
            catalogs[provider] = parse_opencode_models_text(
                path.read_text(encoding="utf-8"),
                provider,
                source="fixture",
            )
    if require_cli:
        for provider in providers:
            if provider in catalogs:
                continue
            raw = fetch_cli_catalog(provider, opencode_bin=opencode_bin, env=env)
            catalogs[provider] = parse_opencode_models_text(raw, provider, source="cli")
    return catalogs
