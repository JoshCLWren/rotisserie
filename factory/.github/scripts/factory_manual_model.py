#!/usr/bin/env python3
"""Verify an operator-pinned model against its provider's live executable catalog.

This is an execution-boundary check, not a permission to bypass provider credit,
quota, store exclusions, worker identity, or assignment eligibility.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from factory_provider_registry import PROVIDERS
from factory_worker_slots import sources


class ManualModelUnavailable(ValueError):
    pass


def validate_model_syntax(source: str, runtime_model: str) -> None:
    if not isinstance(runtime_model, str) or not re.fullmatch(r'[^\s\x00-\x1f]+/[^\s\x00-\x1f]+', runtime_model):
        raise ManualModelUnavailable('Invalid provider/model identifier')
    if source == 'opencode-free':
        prefix = 'opencode-go/'
    elif source == 'openrouter-free':
        prefix = 'openrouter/'
    elif source == 'kilo-auto':
        prefix = 'kilo/'
    elif source in PROVIDERS:
        prefix = PROVIDERS[source].provider + '/'
    else:
        raise ManualModelUnavailable(f'Provider does not support exact-model selection: {source}')
    if not runtime_model.startswith(prefix) or runtime_model == prefix:
        raise ManualModelUnavailable(f'Model does not belong to provider {source}')
    if source == 'openrouter-free' and not runtime_model.endswith(':free'):
        raise ManualModelUnavailable('OpenRouter free lane requires a published :free model')


def select_exact(source: str, runtime_model: str, exclusions: dict[str, list[str]]) -> dict[str, str]:
    validate_model_syntax(source, runtime_model)
    model = runtime_model.split('/', 1)[1]
    if source == 'opencode-free':
        from factory_opencode_go import catalog, free_models
        candidates = set(free_models(catalog()))
        if model not in candidates:
            raise ManualModelUnavailable('Exact model is not a current zero-price tool-capable Go model')
    elif source == 'kilo-auto':
        from factory_kilo_catalog import _selectors
        try:
            data = subprocess.run(
                ['kilo', 'models', 'kilo', '--refresh'], check=True,
                capture_output=True, text=True, timeout=40,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ManualModelUnavailable('Kilo live catalog unavailable') from exc
        selectors = _selectors(data)
        models = {selector: canonical for canonical, selector in selectors.items()}
        if runtime_model not in models:
            raise ManualModelUnavailable('Exact Kilo model missing from current CLI catalog')
        model = models[runtime_model]
    elif source == 'openrouter-free':
        from factory_published_model_catalog import catalog_model_ids, published_catalog
        if model not in catalog_model_ids(source, published_catalog(source)):
            raise ManualModelUnavailable('Exact free OpenRouter model not in published tool-compatible catalog')
    else:
        from factory_provider_candidates import discover
        from factory_runtime_model_select import provider_catalog
        models = discover(source, json.dumps(provider_catalog(source, __import__('os').environ))).candidates
        if not any(row.model == model and row.runtime_model == runtime_model for row in models):
            raise ManualModelUnavailable('Exact model missing from current provider chat/tool catalog')
    if model in exclusions.get(source, []):
        raise ManualModelUnavailable('Exact model is currently excluded by trusted store cooldown')
    return {
        'source': source,
        'model': model,
        'runtime_model': runtime_model,
        'reason': 'operator-pinned-live-catalog-verified',
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, choices=sorted(sources()))
    parser.add_argument('--model', required=True)
    parser.add_argument('--exclusions-file', required=True, type=Path)
    args = parser.parse_args()
    try:
        exclusions = json.loads(args.exclusions_file.read_text(encoding='utf-8'))
        if not isinstance(exclusions, dict) or any(
            not isinstance(values, list) or not all(isinstance(s, str) for s in values)
            for values in exclusions.values()
        ):
            raise ManualModelUnavailable('Invalid trusted exclusion snapshot')
        result = select_exact(args.source, args.model, exclusions)
    except (ManualModelUnavailable, OSError, ValueError, RuntimeError) as exc:
        print(f'Exact model unavailable: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
