"""Which models a provider offers right now, from live sources on this machine.

The worker re-checks its provider's live catalog at start and refuses a model
the provider no longer offers. Checking the same lists before choosing a model
avoids starting workers that can only fail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rotisserie.factory_console import runtime


def opencode_catalog() -> list[str]:
    """Model IDs from the local OpenCode CLI (``opencode models``); empty when unavailable."""
    for candidate in ("opencode", str(Path.home() / ".opencode/bin/opencode")):
        executable = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if executable is None:
            continue
        try:
            output = subprocess.run(
                [executable, "models"], capture_output=True, text=True, timeout=45, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        return [
            line.split()[0]
            for line in output.splitlines()
            if line.strip() and "/" in line.split()[0]
        ]
    return []


def discover_models(provider: runtime.Provider) -> list[tuple[str, str]]:
    """Models from live sources, each labelled with where it came from."""
    found: dict[str, str] = {}
    if provider.model_prefix:
        for model in opencode_catalog():
            if model.startswith(provider.model_prefix):
                if provider.source == "openrouter-free" and not model.endswith(":free"):
                    continue
                found.setdefault(model, "listed by your local OpenCode")
        try:
            for model in runtime.published_models(provider):
                found.setdefault(
                    f"{provider.model_prefix}{model}"
                    if not model.startswith(provider.model_prefix)
                    else model,
                    "listed by the provider",
                )
        except Exception:  # noqa: BLE001 - public catalogs are best effort
            pass
    return sorted(found.items())


def live_models(provider: runtime.Provider) -> frozenset[str] | None:
    """Exact model IDs the provider lists now, or None when no live list is readable here."""
    models = frozenset(model for model, _ in discover_models(provider))
    return models or None
