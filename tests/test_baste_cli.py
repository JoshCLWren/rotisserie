"""``baste`` is the factory console without the ``rotisserie factory`` prefix."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import pytest

from rotisserie import baste


def test_baste_parses_factory_commands_at_the_top_level() -> None:
    arguments = baste.parser().parse_args(["factory", "--json", "why", "issue:12"])
    assert arguments.command == "factory"
    assert arguments.factory_command == "why"
    assert arguments.json is True


def test_baste_routes_every_word_to_the_factory_console(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []

    def console(arguments: argparse.Namespace) -> int:
        seen.append(arguments)
        return 0

    monkeypatch.setattr("rotisserie.baste.factory_commands.main", console)
    assert baste.main(["status"]) == 0
    assert seen[0].factory_command == "status"


def test_baste_web_opens_the_local_control_room(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def rotisserie(arguments: Sequence[str]) -> int:
        seen.append(list(arguments))
        return 0

    monkeypatch.setattr("rotisserie.cli.main", rotisserie)
    assert baste.main(["web", "--port", "9000"]) == 0
    assert seen == [["web", "--port", "9000"]]


def test_baste_help_names_baste(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        baste.main(["status", "--help"])
    assert capsys.readouterr().out.startswith("usage: baste status")
