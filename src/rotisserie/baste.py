"""``baste``: the short name for the factory console.

``baste status`` runs ``rotisserie factory status``; ``baste web`` opens the
local control room. Rotisserie's original local-config commands stay under
``rotisserie --config FILE``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from rotisserie import cli
from rotisserie.factory_console import commands as factory_commands

EPILOG = "Also: 'baste web' opens the local control room."


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="baste")
    factory_commands.add_parser(root.add_subparsers(dest="command"), prog="baste", epilog=EPILOG)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["web"]:
        return cli.main(arguments)
    return factory_commands.main(parser().parse_args(["factory", *arguments]))


if __name__ == "__main__":
    raise SystemExit(main())
