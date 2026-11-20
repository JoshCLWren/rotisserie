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
from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    credential: str
    endpoint: str
    name: str
    additional_credentials: tuple[str, ...] = ()
    headers: tuple[tuple[str, str], ...] = ()



# Provider IDs, credential names, and endpoints belong here, not in workflows.
# Retired 2026-11-17 (owner rule: drop a provider only when ALL its models are
# paid-only): requesty refused every model (555x "balance is too low").
# This is declarative integration metadata, not a model-name guesser.
PROVIDERS: Mapping[str, ProviderSpec] = {
    "nvidia": ProviderSpec("nvidia", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "NVIDIA published API models"),
    "z-ai": ProviderSpec("z-ai", "Z_AI_API_KEY", "https://api.z.ai/api/paas/v4", "Z.AI free capacity"),
    "ollama-cloud": ProviderSpec("ollama-cloud", "OLLAMA_API_KEY", "https://ollama.com/v1", "Ollama Cloud free capacity"),
    "vercel-ai-gateway": ProviderSpec("vercel", "AI_GATEWAY_API_KEY", "https://ai-gateway.vercel.sh/v1", "Vercel AI Gateway free capacity"),
    "mistral": ProviderSpec("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1", "Mistral Codestral"),
    "orcarouter": ProviderSpec("orcarouter", "ORCAROUTER_API_KEY", "https://api.orcarouter.ai/v1", "OrcaRouter free capacity"),
    "pollinations": ProviderSpec("pollinations", "POLLINATIONS_API_KEY", "https://gen.pollinations.ai/v1", "Pollinations AI"),
    "gemini-free": ProviderSpec("google", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "Google Gemini API Free", headers=(("x-goog-api-client", "comic-pile-factory-oai/1.0"),)),
    "groq-free": ProviderSpec("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "GroqCloud Free"),
    "aion-free": ProviderSpec("aion", "AION_API_KEY", "https://api.aionlabs.ai/v1", "Aion Labs Daily Free Credit"),
    "cohere-eval": ProviderSpec("cohere", "COHERE_API_KEY", "https://api.cohere.ai/compatibility/v1", "Cohere Evaluation Key"),
    "typhoon-free": ProviderSpec("typhoon", "TYPHOON_API_KEY", "https://api.opentyphoon.ai/v1", "Typhoon Free API"),
    "cloudflare-workers-ai": ProviderSpec("cloudflare-workers-ai", "CLOUDFLARE_API_TOKEN", "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1", "Cloudflare Workers AI Free", additional_credentials=("CLOUDFLARE_ACCOUNT_ID",)),
}

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
    endpoint = spec.endpoint
    if spec.additional_credentials:
        endpoint = endpoint.replace("{account_id}", env["CLOUDFLARE_ACCOUNT_ID"])
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
