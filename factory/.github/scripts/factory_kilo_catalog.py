#!/usr/bin/env python3
"""Choose a Kilo CLI model from its live 'kilo models' output."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


# Kilo CLI 7.x publishes the anonymous Kilo Auto routes under the generic
# `kilo` provider (`kilo/kilo-auto/free`); older builds used a dedicated
# `kilo-auto` provider (`kilo-auto/free`). `kilo models kilo-auto` now fails
# with "Provider not found: kilo-auto", so both shapes are recognised. The
# canonical model identity stays `kilo-auto/<route>` so Neon health and
# cooldown history keep their keys; only the runtime selector differs.
KILO_PROVIDER = "kilo"
LEGACY_PROVIDER = "kilo-auto"
ROUTE_FAMILY = "kilo-auto/"


def _selectors(catalog_text: str) -> dict[str, str]:
    """Map canonical `kilo-auto/<route>` model IDs to their CLI selector."""
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    found: dict[str, str] = {}
    for raw in catalog_text.splitlines():
        selector = ansi.sub("", raw).strip()
        if "/" not in selector or any(char.isspace() for char in selector):
            continue
        provider, model = selector.split("/", 1)
        if provider == KILO_PROVIDER and model.startswith(ROUTE_FAMILY):
            if model != ROUTE_FAMILY:
                found[model] = selector
        elif provider == LEGACY_PROVIDER and model:
            found.setdefault(selector, selector)
    return found


def discover_models(catalog_text: str) -> list[str]:
    """Preserve published Kilo Auto selectors; never manufacture a model name."""
    return sorted(_selectors(catalog_text))


def choose(
    catalog_text: str, worker: str, excluded: set[str] | None = None,
) -> dict[str, str]:
    selectors = _selectors(catalog_text)
    candidates = [item for item in sorted(selectors) if item not in (excluded or set())]
    if not candidates:
        raise ValueError("Kilo CLI reported no eligible catalog candidates")
    offset = int.from_bytes(hashlib.sha256(worker.encode()).digest()[:8], "big")
    selected = candidates[offset % len(candidates)]
    return {"model": selected, "runtime_model": selectors[selected], "source": "kilo-auto",
            "reason": "selected-from-live-kilo-cli"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--exclusions-file", type=Path)
    args = parser.parse_args()
    try:
        excluded: set[str] = set()
        if args.exclusions_file:
            data = json.loads(args.exclusions_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("kilo-auto", []), list):
                raise ValueError("invalid model exclusion projection")
            excluded = set(data.get("kilo-auto", []))
        result = choose(args.catalog.read_text(encoding="utf-8"), args.worker, excluded)
    except (OSError, ValueError) as exc:
        print(f"Kilo catalog: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
