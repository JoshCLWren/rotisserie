#!/usr/bin/env python3
"""Providers and models the operator turned off, read from repository variables.

``rotisserie factory providers disable NAME`` and ``models disable PROVIDER
MODEL`` write two repository variables:

- ``FACTORY_DISABLED_PROVIDERS``: comma list of provider names, for example
  ``local-ollama,kilo-auto``;
- ``FACTORY_DISABLED_MODELS``: comma list of ``PROVIDER=MODEL`` pairs, with the
  model ID as ``rotisserie factory models PROVIDER`` shows it.

Every factory workflow receives both through ``FACTORY_REPO_VARS``, so the next
dispatcher tick obeys a change without a deploy. Values are restricted to
characters that cannot break that JSON payload. Reasons live in
``FACTORY_DISABLED_REASONS``, which only the operator console reads.

A disable never stops a run that already started. Only a request that names
the provider (or the exact model) explicitly may still use it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping

from factory_settings import repository_variables

PROVIDERS_VAR = "FACTORY_DISABLED_PROVIDERS"
MODELS_VAR = "FACTORY_DISABLED_MODELS"
REASONS_VAR = "FACTORY_DISABLED_REASONS"
ALIASES = {"opencode-go": "opencode-free"}
# Switchable lanes that are not worker sources in factory_worker_slots.
EXTRA_PROVIDERS = {
    "local-ollama": "Local Ollama reviewer (the owner's machine)",
    "routeway": "Routeway (PR-Agent advisory review)",
    "requesty": "Requesty (PR-Agent advisory review)",
}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:+@-]{0,179}$")


def canonical(name: str) -> str:
    value = name.strip().lower()
    return ALIASES.get(value, value)


def _raw(name: str, env: Mapping[str, str] | None) -> str:
    values = os.environ if env is None else env
    if values.get(name):
        return values[name]
    return repository_variables(env).get(name, "")


def parse_providers(raw: str) -> list[str]:
    return [canonical(part) for part in raw.split(",") if NAME_RE.fullmatch(canonical(part))]


def parse_models(raw: str) -> list[tuple[str, str]]:
    pairs = []
    for part in raw.split(","):
        provider, _, model = part.strip().partition("=")
        if NAME_RE.fullmatch(canonical(provider)) and MODEL_RE.fullmatch(model):
            pairs.append((canonical(provider), model))
    return pairs


def format_providers(names: Iterable[str]) -> str:
    return ",".join(sorted(set(names)))


def format_models(pairs: Iterable[tuple[str, str]]) -> str:
    return ",".join(f"{provider}={model}" for provider, model in sorted(set(pairs)))


def disabled_providers(env: Mapping[str, str] | None = None) -> frozenset[str]:
    return frozenset(parse_providers(_raw(PROVIDERS_VAR, env)))


def disabled_models(env: Mapping[str, str] | None = None) -> frozenset[tuple[str, str]]:
    return frozenset(parse_models(_raw(MODELS_VAR, env)))


def model_forms(model: str) -> set[str]:
    """The shown ID and the bare catalog ID that exclusion files use."""
    forms = {model}
    if "/" in model:
        forms.add(model.split("/", 1)[1])
    return forms


def provider_reason(source: str, env: Mapping[str, str] | None = None) -> str | None:
    name = canonical(source)
    if name not in disabled_providers(env):
        return None
    return f"provider {name} is turned off (rotisserie factory providers enable {name})"


def filter_workers(
    workers: list[str],
    sources: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    off = disabled_providers(env)
    kept = [worker for worker in workers if canonical(sources.get(worker, "")) not in off]
    deferred = [
        f"Factory {worker}: provider {canonical(sources.get(worker, ''))} is turned off; no claim"
        for worker in workers
        if worker not in kept
    ]
    return kept, deferred


def filter_sources(sources: Iterable[str], env: Mapping[str, str] | None = None) -> list[str]:
    off = disabled_providers(env)
    return [source for source in sources if canonical(source) not in off]


def merge_exclusions(
    exclusions: Mapping[str, list[str]],
    env: Mapping[str, str] | None = None,
    requested_model: str = "auto",
) -> dict[str, list[str]]:
    """Add turned-off models to a trusted exclusion snapshot.

    An exact model the operator requested stays selectable.
    """
    merged = {source: list(models) for source, models in exclusions.items()}
    for source, model in disabled_models(env):
        if model == requested_model:
            continue
        present = merged.setdefault(source, [])
        present.extend(sorted(form for form in model_forms(model) if form not in present))
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "list"))
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    if args.command == "list":
        print(
            json.dumps(
                {
                    "providers": sorted(disabled_providers()),
                    "models": [list(pair) for pair in sorted(disabled_models())],
                }
            )
        )
        return 0
    if not args.name:
        parser.error("check requires a provider name")
    reason = provider_reason(args.name)
    if reason is not None:
        print(f"[factory-fleet-switch] {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
