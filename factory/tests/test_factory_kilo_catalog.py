"""Kilo model choices come from the CLI, not pinned source files."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github/scripts"))
KILO = importlib.import_module("factory_kilo_catalog")


def test_cli_models_are_discovered_without_allowed_id_list() -> None:
    catalog = "kilo-auto/synthetic-model-a\nkilo-auto/synthetic-model-b\nother/non-kilo-model\n"
    assert KILO.discover_models(catalog) == ["kilo-auto/synthetic-model-a", "kilo-auto/synthetic-model-b"]


def test_selection_respects_cooldowns_but_not_price_or_roster() -> None:
    catalog = "kilo-auto/synthetic-model-a\nkilo-auto/synthetic-model-b\nother/non-kilo-model\n"
    selected = KILO.choose(catalog, "123", {"kilo-auto/synthetic-model-a"})
    assert selected["runtime_model"] == "kilo-auto/synthetic-model-b"
    with pytest.raises(ValueError):
        KILO.choose(catalog, "123", {"kilo-auto/synthetic-model-a", "kilo-auto/synthetic-model-b"})


def test_current_cli_publishes_kilo_auto_routes_under_the_kilo_provider() -> None:
    """Kilo CLI 7.x: `kilo models kilo-auto` fails with "Provider not found".

    The routes now appear as `kilo/kilo-auto/<route>` beside unrelated `kilo/*`
    models. The canonical model stays `kilo-auto/<route>` (Neon health keys);
    the runtime selector is the exact CLI string.
    """
    catalog = (
        "\x1b[32mkilo/kilo-auto/free\x1b[0m\n"
        "kilo/kilo-auto/small\n"
        "kilo/kilo-auto/\n"
        "kilo/poolside/laguna-xs-2.1:free\n"
        "github-copilot/some-model\n"
    )
    assert KILO.discover_models(catalog) == ["kilo-auto/free", "kilo-auto/small"]
    selected = KILO.choose(catalog, "123", {"kilo-auto/small"})
    assert selected["model"] == "kilo-auto/free"
    assert selected["runtime_model"] == "kilo/kilo-auto/free"
    assert selected["source"] == "kilo-auto"
