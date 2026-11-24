"""Turn providers and models off or on for the whole fleet.

The state lives in repository variables (see ``factory_fleet_switch`` in the
factory runtime), so the next dispatcher tick obeys a change without a deploy.
Nothing here cancels a run that already started.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError, GitHubNotFoundError
from rotisserie.factory_console.service import providers

MAX_REASON = 200


class SwitchError(ValueError):
    """The operator named something the fleet switch cannot turn off or on."""


def _switch() -> ModuleType:
    return runtime.module("factory_fleet_switch")


@dataclass(frozen=True)
class Off:
    kind: str  # provider | model
    provider: str  # canonical factory name (worker source, or a lane like local-ollama)
    model: str | None
    reason: str
    since: str

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "provider": display_name(self.provider),
            "model": self.model,
            "reason": self.reason or None,
            "since": self.since or None,
        }


@dataclass(frozen=True)
class FleetState:
    providers: dict[str, Off]
    models: dict[tuple[str, str], Off]

    def provider_off(self, name: str) -> Off | None:
        return self.providers.get(canonical(name))

    def model_off(self, provider: str, model: str) -> Off | None:
        return self.models.get((canonical(provider), model))

    def to_dict(self) -> dict[str, object]:
        return {
            "disabled_providers": [item.to_dict() for item in self.providers.values()],
            "disabled_models": [item.to_dict() for item in self.models.values()],
        }


def canonical(name: str) -> str:
    return str(_switch().canonical(name))


def display_name(name: str) -> str:
    """The provider key the operator types (``opencode-go``, not ``opencode-free``)."""
    found = next((item for item in runtime.providers() if item.source == name), None)
    return found.key if found else name


def extra_lanes() -> dict[str, str]:
    """Switchable lanes outside the worker fleet, such as the local Ollama reviewer."""
    return dict(_switch().EXTRA_PROVIDERS)


def _notes(variables: Mapping[str, str]) -> dict[str, dict[str, str]]:
    try:
        parsed = json.loads(variables.get(_switch().REASONS_VAR, "") or "{}")
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key): {str(k): str(v) for k, v in value.items()}
        for key, value in parsed.items()
        if isinstance(value, dict)
    }


def state(variables: Mapping[str, str]) -> FleetState:
    module = _switch()
    env = {name: variables.get(name, "") for name in (module.PROVIDERS_VAR, module.MODELS_VAR)}
    notes = _notes(variables)
    off_providers = {}
    for name in sorted(module.disabled_providers(env)):
        note = notes.get(f"provider:{name}", {})
        off_providers[name] = Off(
            "provider", name, None, note.get("reason", ""), note.get("at", "")
        )
    off_models = {}
    for provider, model in sorted(module.disabled_models(env)):
        note = notes.get(f"model:{provider}={model}", {})
        off_models[(provider, model)] = Off(
            "model", provider, model, note.get("reason", ""), note.get("at", "")
        )
    return FleetState(off_providers, off_models)


def read(github: GitHub) -> FleetState:
    return state(providers.repository_variables(github))


def _current(github: GitHub) -> dict[str, str]:
    """Live repository variables; a failed read stops the write instead of dropping entries."""
    try:
        rows = json.loads(
            github.gh("variable", "list", "--repo", github.repo, "--json", "name,value")
        )
    except ValueError:
        raise GitHubError("GitHub returned an unreadable variable list") from None
    return {str(row["name"]): str(row["value"]) for row in rows if isinstance(row, dict)}


def switchable_provider(name: str) -> str:
    """Canonical name for a provider the fleet switch knows, or raise SwitchError."""
    wanted = canonical(name)
    if wanted in extra_lanes():
        return wanted
    try:
        return runtime.resolve_provider(name).source
    except ValueError:
        known = sorted({item.key for item in runtime.providers()} | set(extra_lanes()))
        raise SwitchError(f"Unknown provider {name!r}. Available: {', '.join(known)}") from None


def full_model_id(provider: runtime.Provider, model: str) -> str:
    """The model ID as ``rotisserie factory models PROVIDER`` shows it."""
    if model == "auto":
        raise SwitchError("Name an exact model; 'auto' is not a model")
    full = (
        model
        if not provider.model_prefix or model.startswith(provider.model_prefix)
        else provider.model_prefix + model
    )
    if not _switch().MODEL_RE.fullmatch(full):
        raise SwitchError(f"{model!r} is not a model ID the fleet switch can store")
    try:
        runtime.validate_model(provider, full)
    except ValueError as exc:
        raise SwitchError(str(exc)) from None
    return full


def _write(github: GitHub, name: str, value: str) -> None:
    if value:
        github.gh("variable", "set", name, "--repo", github.repo, "--body", value)
        return
    try:
        github.gh("variable", "delete", name, "--repo", github.repo)
    except GitHubNotFoundError:
        pass


def _save_note(
    github: GitHub,
    variables: Mapping[str, str],
    key: str,
    reason: str,
    now: datetime,
    *,
    enabled: bool,
) -> None:
    notes = _notes(variables)
    if enabled:
        if notes.pop(key, None) is None:
            return
    else:
        notes[key] = {"reason": reason[:MAX_REASON], "at": now.isoformat(timespec="seconds")}
    _write(
        github,
        _switch().REASONS_VAR,
        json.dumps(notes, sort_keys=True) if notes else "",
    )


@dataclass(frozen=True)
class Change:
    kind: str
    provider: str
    model: str | None
    enabled: bool
    changed: bool
    state: FleetState

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "provider": display_name(self.provider),
            "model": self.model,
            "enabled": self.enabled,
            "changed": self.changed,
            **self.state.to_dict(),
        }


def set_provider(github: GitHub, name: str, *, enabled: bool, reason: str, now: datetime) -> Change:
    module = _switch()
    variables = _current(github)
    current = set(state(variables).providers)
    target = (
        canonical(name) if enabled and canonical(name) in current else switchable_provider(name)
    )
    wanted = current - {target} if enabled else current | {target}
    changed = wanted != current
    if changed:
        _write(github, module.PROVIDERS_VAR, module.format_providers(wanted))
    _save_note(github, variables, f"provider:{target}", reason, now, enabled=enabled)
    return Change("provider", target, None, enabled, changed, read(github))


def set_model(
    github: GitHub, provider_name: str, model: str, *, enabled: bool, reason: str, now: datetime
) -> Change:
    module = _switch()
    try:
        provider = runtime.resolve_provider(provider_name)
    except ValueError as exc:
        raise SwitchError(str(exc)) from None
    variables = _current(github)
    current = set(state(variables).models)
    pair = (provider.source, model)
    if not (enabled and pair in current):
        pair = (provider.source, full_model_id(provider, model))
    wanted = current - {pair} if enabled else current | {pair}
    changed = wanted != current
    if changed:
        _write(github, module.MODELS_VAR, module.format_models(wanted))
    _save_note(github, variables, f"model:{pair[0]}={pair[1]}", reason, now, enabled=enabled)
    return Change("model", provider.source, pair[1], enabled, changed, read(github))
