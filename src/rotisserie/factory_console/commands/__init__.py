"""``rotisserie factory``: the operator console for the software factory."""

from __future__ import annotations

from rotisserie.factory_console.commands._shared import EXIT_INTERRUPTED
from rotisserie.factory_console.commands.entry import main, run_cli
from rotisserie.factory_console.commands.parser import add_parser

__all__ = ["EXIT_INTERRUPTED", "add_parser", "main", "run_cli"]
