"""Read provider, worker and settings metadata from the factory runtime itself.

The operator console never keeps its own provider list, worker roster or
settings table. It loads the same modules the dispatcher overlays into adopter
repositories (``factory/.github/scripts``). Wheels carry a copy of those files
under ``rotisserie/factory_console/factory_runtime``; a source checkout reads
them in place. The dispatcher still validates everything at execution time.
"""

from __future__ import annotations

import functools
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

RUNTIME_MODULES = (
    "factory_provider_registry",
    "factory_worker_slots",
    "factory_manual_model",
    "factory_settings",
    "factory_dispatch_credential_filter",
    "factory_published_model_catalog",
)

# Factory provider keys that the GitHub dispatch accepts under another name.
PROVIDER_ALIASES = {"opencode-go": "opencode-free", "opencode-free": "opencode-free"}


class RuntimeUnavailableError(RuntimeError):
    """The packaged factory runtime metadata could not be loaded."""


def runtime_directory() -> Path:
    packaged = Path(__file__).with_name("factory_runtime")
    if (packaged / "factory_worker_slots.py").is_file():
        return packaged
    checkout = Path(__file__).resolve().parents[3] / "factory" / ".github" / "scripts"
    if (checkout / "factory_worker_slots.py").is_file():
        return checkout
    raise RuntimeUnavailableError(
        "This Rotisserie installation is missing its factory runtime files; reinstall it."
    )


@functools.cache
def module(name: str) -> ModuleType:
    if name not in RUNTIME_MODULES:
        raise RuntimeUnavailableError(f"{name} is not a factory runtime module")
    directory = str(runtime_directory())
    inserted = directory not in sys.path
    if inserted:
        sys.path.insert(0, directory)
    try:
        return importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover - packaging defect
        raise RuntimeUnavailableError(f"factory runtime module {name} failed to load") from exc
    finally:
        if inserted:
            sys.path.remove(directory)


@dataclass(frozen=True)
class Provider:
    """One factory provider integration, in operator vocabulary."""

    key: str  # what the operator types and the dispatcher accepts
    source: str  # the factory's internal source key (worker identities, Neon)
    name: str
    model_prefix: str  # exact model IDs for this provider start with this
    secrets: tuple[str, ...]  # GitHub secret names it needs; empty means none
    needs_variable: str = ""  # an Actions variable that must be enabled


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def providers() -> list[Provider]:
    """Every provider the factory runtime integrates, discovered not listed."""
    slots = module("factory_worker_slots")
    registry = module("factory_provider_registry")
    credentials = module("factory_dispatch_credential_filter")
    manual = module("factory_manual_model")
    sources: tuple[str, ...] = tuple(str(item) for item in slots.sources())
    specs = registry.PROVIDERS
    result: list[Provider] = []
    for source in sources:
        key = "opencode-go" if source == "opencode-free" else source
        spec = specs.get(source)
        name = _string(getattr(spec, "name", "")) or _NATIVE_NAMES.get(source, source)
        prefix = _model_prefix(manual, source)
        requirements = credentials.source_requirements(source)
        secrets: list[str] = []
        variable = ""
        if source not in credentials.ANONYMOUS and requirements is not None:
            for flag in requirements:
                flag_name = str(flag)
                if flag_name == "FACTORY_OMNIROUTE_ENABLED":
                    variable = flag_name
                elif flag_name.startswith("FACTORY_HAS_"):
                    secrets.append(flag_name.removeprefix("FACTORY_HAS_"))
        result.append(Provider(key, source, name, prefix, tuple(secrets), variable))
    return sorted(result, key=lambda item: item.key)


# Display names for OpenCode-native integrations that have no registry entry.
_NATIVE_NAMES = {
    "opencode-free": "OpenCode Go",
    "openrouter-free": "OpenRouter free models",
    "kilo-auto": "Kilo",
    "omniroute-free": "OmniRoute",
}


def _model_prefix(manual: ModuleType, source: str) -> str:
    """Ask the runtime's own exact-model validator which prefix it accepts.

    Returns "" when the provider only supports automatic model choice.
    """
    spec = module("factory_provider_registry").PROVIDERS.get(source)
    candidates = ["opencode-go", "openrouter", "kilo", _string(getattr(spec, "provider", ""))]
    suffix = ":free" if source == "openrouter-free" else ""
    for candidate in filter(None, candidates):
        try:
            manual.validate_model_syntax(source, f"{candidate}/probe{suffix}")
        except ValueError:
            continue
        return candidate + "/"
    return ""


def resolve_provider(value: str) -> Provider:
    wanted = PROVIDER_ALIASES.get(value, value)
    for provider in providers():
        if provider.key == value or provider.source == wanted:
            return provider
    known = ", ".join(provider.key for provider in providers())
    raise ValueError(f"Unknown provider {value!r}. Available: {known}")


def validate_model(provider: Provider, model: str) -> None:
    """Apply the dispatcher's own exact-model syntax rule before submitting."""
    if model == "auto":
        return
    module("factory_manual_model").validate_model_syntax(provider.source, model)


def worker_sources() -> dict[str, str]:
    """Every valid worker identity (regular and operator overflow) to its source."""
    identities = module("factory_worker_slots").all_identities()
    return {str(worker): str(source) for worker, source in identities.items()}


def provider_for_worker(worker: str) -> Provider | None:
    source = worker_sources().get(worker)
    if source is None:
        return None
    return next((item for item in providers() if item.source == source), None)


def provider_for_source(source: str) -> str:
    """Display name for a factory source key."""
    found = next((item for item in providers() if item.source == source), None)
    return found.name if found else source


def regular_slot_count() -> int:
    return int(module("factory_worker_slots").slot_count({}))


def worker_slot_number(worker: str) -> int | None:
    """One-based slot number for display, or None for an unknown identity."""
    try:
        slot = module("factory_worker_slots").resolve(worker, {})
    except ValueError:
        return None
    return int(slot.index) + 1


@dataclass(frozen=True)
class SettingInfo:
    name: str
    default: str
    title: str
    description: str
    unit: str
    minimum: int
    maximum: int
    group: str


def settings() -> list[SettingInfo]:
    registry = module("factory_settings")
    return [
        SettingInfo(
            str(item.name),
            str(item.default),
            str(item.title),
            str(item.description),
            str(item.unit),
            int(item.minimum),
            int(item.maximum),
            str(item.group),
        )
        for item in registry.SETTINGS
    ]


def validate_setting(name: str, value: str) -> str:
    registry = module("factory_settings")
    if name not in registry.BY_NAME:
        raise ValueError(f"Unknown setting {name}")
    return str(registry.validate(registry.BY_NAME[name], value))


def published_models(provider: Provider) -> list[str]:
    """Models a provider publicly lists, when it has a public catalog."""
    catalog = module("factory_published_model_catalog")
    if provider.source not in catalog.PUBLIC_CATALOGS:
        return []
    payload = catalog.published_catalog(provider.source, timeout=10)
    return [str(item) for item in catalog.catalog_model_ids(provider.source, payload)]
