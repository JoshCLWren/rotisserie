"""Every `baste` command has a control in both control rooms, or a reason not to.

AGENTS.md: a new or changed CLI command gets a working control in the local
web UI and the hosted control room in the same PR. This test enforces it from
the CLI parser, so a new subcommand fails here until it is mapped or excluded.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rotisserie import control_contract as contract
from rotisserie.control_parity import CLI_ONLY, CONTROLS
from rotisserie.factory_console.commands.parser import add_parser

ROOT = Path(__file__).resolve().parents[1]
UI = "\n".join(
    (ROOT / "src" / "rotisserie" / name).read_text(encoding="utf-8")
    for name in ("web_ui.html", "web_ui.js")
)
BUILDER = ROOT / "factory" / "scripts" / "build_hosted_control_room.py"

READ_SAMPLES: dict[str, dict[str, str]] = {
    "models": {"provider": "opencode-go"},
    "checks": {"target": "pr:4"},
    "review": {"target": "pr:4"},
    "conflicts": {"target": "pr:4"},
    "runs": {"target": "issue:4"},
    "history": {"target": "issue:4"},
    "estimate": {"target": "issue:4", "run": "123"},
    "follow": {"target": "issue:4"},
    "store": {"mode": "selftest", "run": "123"},
}
WRITE_SAMPLES: dict[str, dict[str, Any]] = {
    "run": {"provider": "opencode-go"},
    "cancel": {"target": "pr:4"},
    "prioritize": {"target": "issue:4"},
    "sync": {"target": "pr:4"},
    "allow_self_review": {"target": "pr:4"},
    "settings": {"name": "FACTORY_PR_WIP_LIMIT", "value": "3"},
    "provider_switch": {"provider": "opencode-go", "enabled": True},
    "model_switch": {"provider": "opencode-go", "model": "m", "enabled": True},
    "incident_start": {"issue": "issue:4"},
    "incident_stop": {"issue": "issue:4"},
    "claim": {"target": "issue:4"},
    "finish": {"target": "issue:4"},
    "close": {"target": "pr:4", "reason": "duplicate"},
    "backoffs_reset": {},
    "estimate_neon": {"target": "issue:4"},
    "assign": {"target": "pr:4", "agent": "claude"},
}


def subcommands(register: Callable[[Any], None] = add_parser) -> set[str]:
    """Primary subcommand names. ``choices`` also holds parsers registered without help."""
    root = argparse.ArgumentParser()
    register(root.add_subparsers())
    factory = next(
        action for action in root._actions if isinstance(action, argparse._SubParsersAction)
    ).choices["factory"]
    commands = next(
        action for action in factory._actions if isinstance(action, argparse._SubParsersAction)
    )
    primary: dict[int, str] = {}
    for name, parser in commands.choices.items():
        primary.setdefault(id(parser), name)
    return set(primary.values())


def test_a_subcommand_without_help_is_still_found() -> None:
    def register(subparsers: Any) -> None:
        add_parser(subparsers)
        factory = subparsers.choices["factory"]
        commands = next(a for a in factory._actions if isinstance(a, argparse._SubParsersAction))
        commands.add_parser("brand-new")

    found = subcommands(register)
    assert "brand-new" in found
    assert "brand-new" not in set(CONTROLS) | set(CLI_ONLY)
    assert "who" not in found and "status" in found


def mapped(kind: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for command, controls in CONTROLS.items():
        for control in controls:
            prefix, _, name = control.partition(":")
            if prefix == kind:
                found[name] = command
    return found


def hosted_tables() -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("rotisserie_hosted_builder_parity", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    page = module.render("acme/widgets", "https://auth.widgets.example")
    found = re.search(r"const CONTRACT = (\{.*?\});", page)
    assert found is not None
    tables: dict[str, Any] = json.loads(found.group(1))
    return tables


def test_every_cli_command_is_mapped_or_explained() -> None:
    commands = subcommands()
    assert len(commands) > 30
    unaccounted = commands - set(CONTROLS) - set(CLI_ONLY)
    assert not unaccounted, f"map these commands to UI controls or explain them: {unaccounted}"
    assert not set(CONTROLS) & set(CLI_ONLY)
    assert not (set(CONTROLS) | set(CLI_ONLY)) - commands, "stale parity entries"
    for command, reason in CLI_ONLY.items():
        assert len(reason) > 30, command


def test_mapped_controls_cover_the_whole_contract() -> None:
    assert set(mapped("read")) == contract.READ_ONLY
    assert set(mapped("write")) == contract.WRITE_OPERATIONS
    for command, controls in CONTROLS.items():
        assert controls, command
        assert all(c.partition(":")[0] in ("read", "write") for c in controls), command


@pytest.mark.parametrize("name", sorted(contract.READ_ONLY))
def test_each_read_runs_its_cli_command(name: str) -> None:
    args = contract.read_command(name, READ_SAMPLES.get(name, {}))
    assert args[0] == mapped("read")[name]


@pytest.mark.parametrize("name", sorted(contract.WRITE_OPERATIONS))
def test_each_write_runs_its_cli_command(name: str) -> None:
    args = contract.write_command({"operation": name, **WRITE_SAMPLES.get(name, {})})
    assert args[0] == mapped("write")[name]


@pytest.mark.parametrize("name", sorted(contract.READ_ONLY))
def test_local_ui_has_a_control_for_each_read(name: str) -> None:
    markers = (f'get("{name}"', f'data-item-read="{name}"', f'data-pr-inspect="{name}"')
    assert any(marker in UI for marker in markers), f"web_ui.html has no control for {name}"


@pytest.mark.parametrize("name", sorted(contract.WRITE_OPERATIONS))
def test_local_ui_has_a_control_for_each_write(name: str) -> None:
    switch = 'operation:b.dataset.model?"model_switch":"provider_switch"'
    sent = (
        f'operation:"{name}"' in UI
        or f'itemWrite("{name}"' in UI
        or (name.endswith("_switch") and switch in UI)
    )
    assert sent, f"web_ui.html never sends {name}"


def test_hosted_page_routes_every_read_and_write() -> None:
    tables = hosted_tables()
    assert set(tables["public_reads"]) | set(tables["live_reads"]) == contract.READ_ONLY
    assert not set(tables["public_reads"]) & set(tables["live_reads"])
    assert set(tables["write_operations"]) == contract.WRITE_OPERATIONS
