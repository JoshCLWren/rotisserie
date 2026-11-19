#!/usr/bin/env python3
"""Select canonical provider/model pairs across one leased factory assignment.

Runtime and published provider catalogs supply all identifiers. Failure history,
credential availability and trusted provider capacity narrow eligibility without
a static roster or a parallel dispatcher. Switching candidates never changes
the target's ownership or exact-head expectations.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
import shutil
import subprocess


class ExhaustedCandidatesError(ValueError):
    """The provider has no eligible model that has not been attempted."""


# Last outcomes proving the exact model did real coding work on an
# assignment (it ran tools and produced or validated changes). A clean
# success, edits that failed checks, or a full-budget session all qualify.
PROVEN_OUTCOMES = frozenset({"success", "work_failure", "agent_timeout"})


def proven_models(health_json: str | None = None) -> set[tuple[str, str]]:
    """Read the trusted Neon health snapshot handed to every runner.

    Advisory only: it orders already-eligible candidates and can never make
    a cooled, attempted or unlisted model selectable.
    """
    raw = os.environ.get("FACTORY_CANDIDATE_HEALTH", "") if health_json is None else health_json
    try:
        snapshot = json.loads(raw) if raw else {}
    except ValueError:
        return set()
    rows = snapshot.get("models") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        return set()
    return {
        (row["source"], row["model"])
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("source"), str) and isinstance(row.get("model"), str)
        and row.get("outcome") in PROVEN_OUTCOMES
        and row.get("status") not in {"retired", "unavailable"}
    }


def _catalog_ids(
    source: str, catalog_text: str | None, catalog_json: Mapping[str, Any] | None,
) -> tuple[set[str], Mapping[str, Any] | None]:
    """Return every published model id for one provider snapshot."""
    if source == "opencode-free":
        from factory_live_opencode import catalog_models

        if catalog_text is None:
            raise ValueError("trusted OpenCode catalog snapshot missing")
        return set(catalog_models(catalog_text, provider=source)), catalog_json
    if source == "kilo-auto":
        from factory_kilo_catalog import discover_models

        if catalog_text is None:
            raise ValueError("trusted Kilo catalog snapshot missing")
        return set(discover_models(catalog_text)), catalog_json
    if source == "openrouter-free":
        from factory_published_model_catalog import catalog_model_ids, published_catalog

        catalog = catalog_json if catalog_json is not None else published_catalog(source)
        return set(catalog_model_ids(source, catalog)), catalog
    from factory_provider_candidates import discover
    from factory_runtime_model_select import provider_catalog

    catalog = catalog_json if catalog_json is not None else provider_catalog(source, os.environ)
    ids = {candidate.model for candidate in discover(source, json.dumps(catalog)).candidates}
    return ids, catalog


def select_next(
    source: str,
    worker: str,
    attempted: set[str],
    exclusions: Mapping[str, list[str]],
    *,
    catalog_text: str | None = None,
    catalog_json: Mapping[str, Any] | None = None,
    rotation: int = 0,
    proven: set[tuple[str, str]] | None = None,
    proven_only: bool = False,
) -> dict[str, Any]:
    """Choose an unattempted, uncooled model on this provider.

    Models with proven coding outcomes are preferred; with none eligible the
    choice is unchanged. proven_only raises when no proven model is eligible.
    """
    excluded = set(exclusions.get(source, ())) | attempted
    ids, catalog_json = _catalog_ids(source, catalog_text, catalog_json)
    available = ids - excluded
    if not available:
        raise ExhaustedCandidatesError(f"no eligible unattempted {source} models")
    preferred = {
        model for provider, model in (proven_models() if proven is None else proven)
        if provider == source
    } & available
    if proven_only and not preferred:
        raise ExhaustedCandidatesError(f"no eligible proven {source} models")
    if preferred:
        # Narrow the existing selector's choice to proven models only.
        excluded |= available - preferred
    if source == "opencode-free":
        from factory_live_opencode import select_model

        selected = select_model(
            catalog_text or "", int(worker), rotation,
            attempted=excluded, provider=source,
        )
    elif source == "kilo-auto":
        from factory_kilo_catalog import choose

        selected = choose(catalog_text or "", worker, excluded)
    elif source == "openrouter-free":
        from factory_published_model_catalog import choose_model

        model = choose_model(source, worker, catalog_json, excluded)
        selected = {"model": model, "runtime_model": f"openrouter/{model}"}
    else:
        from factory_runtime_model_select import select

        selected = select(source, worker, catalog_json or {}, {source: sorted(excluded)})
    # Every caller reads .source; the OpenCode selector only reports provider.
    return {**selected, "source": source}



def select_next_fleet(
    worker: str,
    original_source: str,
    attempted: set[tuple[str, str]],
    blocked_providers: set[str],
    exclusions: Mapping[str, list[str]],
    catalogs: Mapping[str, Mapping[str, Any] | str],
    eligible_sources: set[str],
    *,
    rotation: int = 0,
    proven: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Select the next provider/model from canonical catalogs, not one source.

    A provider/account breaker skips every model at that source; model-only
    failures exclude precisely one canonical pair. The eligible_sources set
    is the trusted controller's admission projection, not a model roster.
    """
    sources = sorted(eligible_sources & set(catalogs) - blocked_providers)
    if original_source in sources:
        sources.remove(original_source)
        sources.insert(0, original_source)
    proven_pairs = proven_models() if proven is None else proven
    # First pass: a proven model on any eligible provider beats an unproven
    # model on the original provider. Second pass: today's order.
    for proven_only in (True, False):
        for source in sources:
            catalog = catalogs[source]
            excluded = {model for provider, model in attempted if provider == source}
            try:
                candidate = select_next(
                    source, worker, excluded, exclusions,
                    catalog_text=catalog if isinstance(catalog, str) else None,
                    catalog_json=catalog if isinstance(catalog, Mapping) else None,
                    rotation=rotation, proven=proven_pairs, proven_only=proven_only,
                )
            except ExhaustedCandidatesError:
                continue
            if (candidate["source"], candidate["model"]) in attempted:
                raise ValueError("selector returned an attempted canonical pair")
            return candidate
    raise ExhaustedCandidatesError("no eligible provider/model combination remains")



def discover_fleet_catalogs(
    eligible_sources: set[str],
    *,
    cache: Path,
    env: Mapping[str, str],
    existing_catalogs: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Take one canonical snapshot per assignment; never probe model inference.

    Catalog endpoints may fail independently. Missing credentials skip only
    that provider, not its neighbors. Failed catalog reads are not attributed
    to any model's health or quota.
    """
    from factory_provider_registry import PROVIDERS, missing_credentials
    from factory_runtime_model_select import provider_catalog

    if cache.is_file():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("invalid trusted fleet catalog snapshot")
        return saved

    files = existing_catalogs or {}
    def fetch(source: str) -> tuple[str, Any]:
        if source in files and files[source].is_file():
            return source, files[source].read_text(encoding="utf-8")
        if source == "opencode-free":
            if not shutil.which("opencode"):
                raise ValueError("OpenCode CLI missing")
            run = subprocess.run(
                ["opencode", "models", "--standalone"],
                check=True, capture_output=True, text=True, timeout=35,
            )
            return source, run.stdout
        if source == "kilo-auto":
            if not shutil.which("kilo"):
                raise ValueError("Kilo CLI missing")
            run = subprocess.run(
                ["kilo", "models", "kilo", "--refresh"],
                check=True, capture_output=True, text=True, timeout=35,
            )
            return source, run.stdout
        if source == "openrouter-free":
            if not env.get("OPENROUTER_API_KEY"):
                raise ValueError("OpenRouter credentials missing")
            from factory_published_model_catalog import published_catalog
            return source, published_catalog(source)
        spec = PROVIDERS.get(source)
        if spec is None or missing_credentials(spec, env):
            raise ValueError("provider credentials or canonical integration missing")
        if source == "vercel-ai-gateway":
            from factory_published_model_catalog import published_catalog
            return source, published_catalog(source)
        return source, provider_catalog(source, env)

    discovered: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(eligible_sources)))) as executor:
        pending = {executor.submit(fetch, s): s for s in sorted(eligible_sources)}
        for future in as_completed(pending):
            try:
                source, catalog = future.result()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                continue
            discovered[source] = catalog
    cache.write_text(json.dumps(discovered, sort_keys=True), encoding="utf-8")
    return discovered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--eligible-sources-file", type=Path)
    parser.add_argument("--fleet-catalog-cache", type=Path)
    parser.add_argument("--attempted-pair", action="append", default=[])
    parser.add_argument("--blocked-provider", action="append", default=[])
    parser.add_argument("--worker", required=True)
    parser.add_argument("--exclusions-file", type=Path, required=True)
    parser.add_argument("--catalog-file", type=Path)
    parser.add_argument("--catalog-json", type=Path, help="offline catalog fixture")
    parser.add_argument("--catalog-cache", type=Path, help="trusted per-run catalog snapshot")
    parser.add_argument("--attempted", action="append", default=[])
    parser.add_argument("--rotation", type=int, default=0)
    parser.add_argument("--proven-only", action="store_true",
                        help="succeed only with a model whose last outcome proved real work")
    args = parser.parse_args()
    try:
        if not args.worker.isdecimal() or int(args.worker) < 1:
            raise ValueError("invalid worker identity")
        exclusions = json.loads(args.exclusions_file.read_text(encoding="utf-8"))
        if not isinstance(exclusions, dict) or any(
            not isinstance(models, list) or not all(isinstance(x, str) for x in models)
            for models in exclusions.values()
        ):
            raise ValueError("invalid trusted model exclusion projection")
        catalog_text = (
            args.catalog_file.read_text(encoding="utf-8") if args.catalog_file else None
        )
        catalog_json = (
            json.loads(args.catalog_json.read_text(encoding="utf-8"))
            if args.catalog_json else None
        )
        # Fetch once per assignment. Each failed model then advances through the
        # same authoritative published snapshot without hammering the provider.
        if catalog_json is None and args.catalog_cache and args.catalog_cache.is_file():
            catalog_json = json.loads(args.catalog_cache.read_text(encoding="utf-8"))
        if (catalog_json is None and args.catalog_cache
                and args.source not in {"opencode-free", "kilo-auto"}):
            if args.source == "openrouter-free":
                from factory_published_model_catalog import published_catalog
                catalog_json = published_catalog(args.source)
            else:
                from factory_runtime_model_select import provider_catalog
                catalog_json = provider_catalog(args.source, os.environ)
            args.catalog_cache.write_text(json.dumps(catalog_json), encoding="utf-8")
        if args.eligible_sources_file is None:
            selected = select_next(
                args.source, args.worker, set(args.attempted), exclusions,
                catalog_text=catalog_text, catalog_json=catalog_json,
                rotation=args.rotation, proven_only=args.proven_only,
            )
        else:
            sources = json.loads(args.eligible_sources_file.read_text(encoding="utf-8"))
            if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
                raise ValueError("invalid trusted fleet capacity projection")
            if args.fleet_catalog_cache is None:
                raise ValueError("fleet catalog cache missing")
            files = {args.source: args.catalog_file} if args.catalog_file else {}
            catalogs = discover_fleet_catalogs(
                set(sources), cache=args.fleet_catalog_cache,
                env=os.environ, existing_catalogs=files,
            )
            pairs: set[tuple[str, str]] = set()
            for raw in args.attempted_pair:
                pair = json.loads(raw)
                if not isinstance(pair, list) or len(pair) != 2 or not all(
                    isinstance(s, str) for s in pair
                ):
                    raise ValueError("invalid attempted provider/model pair")
                pairs.add((pair[0], pair[1]))
            selected = select_next_fleet(
                args.worker, args.source, pairs, set(args.blocked_provider),
                exclusions, catalogs, set(sources), rotation=args.rotation,
            )
    except ExhaustedCandidatesError as exc:
        print(f"factory failover exhausted: {exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"factory failover error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(selected, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
