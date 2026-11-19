#!/usr/bin/env python3
"""One authoritative mapping from factory quota sources to OpenCode providers.

Factory source keys remain stable for budgets and Neon. The provider/model
selector and compatible API configuration are derived from this registry.
Published upstream model IDs are passed through without prefix rewriting.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping

import factory_config
from factory_config import ProviderSpec


# Providers come from the adopter's .rotisserie/config.toml merged over
# factory/defaults/providers.toml; see factory_config.
PROVIDERS: Mapping[str, ProviderSpec] = factory_config.current().providers

MODEL_ID_RE = re.compile(r"^[^\s\x00-\x1f]+$")


def get_spec(source: str, model: str) -> ProviderSpec:
    if source not in PROVIDERS:
        raise ValueError(f"unknown factory source: {source}")
    if not model or not MODEL_ID_RE.fullmatch(model) or model.startswith("/"):
        raise ValueError("invalid upstream model ID")
    return PROVIDERS[source]


def missing_credentials(spec: ProviderSpec, env: Mapping[str, str]) -> list[str]:
    return [key for key in (spec.credential, *spec.additional_credentials) if not env.get(key)]


def resolve(source: str, model: str, env: Mapping[str, str]) -> dict[str, object]:
    spec = get_spec(source, model)
    missing = missing_credentials(spec, env)
    return {
        "runtime_model": f"{spec.provider}/{model}",
        "branch_suffix": source,
        "configured": not missing,
        "reason": "configured" if not missing else f"missing-{missing[0]}",
        "configure_adapter": True,
    }


def compatible_config(source: str, model: str, env: Mapping[str, str]) -> dict[str, object]:
    spec = get_spec(source, model)
    missing = missing_credentials(spec, env)
    if missing:
        raise ValueError(f"missing provider configuration: {missing[0]}")
    endpoint = spec.resolved_endpoint(env)
    return {"providers": {spec.provider: {
        "name": spec.name,
        "package": "@opencode/ai/providers/openai-compatible",
        "env": [spec.credential],
        "settings": {"baseURL": endpoint},
        "headers": dict(spec.headers),
        "models": {model: {"name": model, "modelID": model}},
    }}}


def credential_status(source: str, env: Mapping[str, str]) -> dict[str, object]:
    """Expose capability without requiring a checked-in model ID."""
    spec = PROVIDERS.get(source)
    if spec is None:
        raise ValueError(f"unknown factory source: {source}")
    missing = missing_credentials(spec, env)
    return {
        "configured": not missing,
        "reason": "configured" if not missing else f"missing-{missing[0]}",
        "branch_suffix": source,
        "configure_adapter": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("resolve", "config", "credentials"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--model")
    args = parser.parse_args()
    try:
        if args.command == "credentials":
            result = credential_status(args.source, os.environ)
        else:
            if not args.model:
                parser.error("this command requires --model")
            result = (
                resolve(args.source, args.model, os.environ)
                if args.command == "resolve"
                else compatible_config(args.source, args.model, os.environ)
            )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
