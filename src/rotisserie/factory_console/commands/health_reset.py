from __future__ import annotations

import argparse

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.why import _bridge_failed, _show_bridge
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import backoffs, bridge
from rotisserie.factory_console.tracker import EXIT_CODES, Result

MODEL_HELP = """\
Clear one model's health bench and backoff rows, and nothing else.

  baste health reset nvidia/moonshotai/kimi-k3.1-pro --via github
  baste health reset MODEL --source nvidia --via github

MODEL is the model ID as 'baste backoffs' shows it, or the source/model form
that run titles print. It deletes the model's factory_model_health rows and
its model backoff rows (for every source, or only --source), and lists each
row it deleted. Worker, provider and pool cooldowns stay, retired (HTTP 410)
records stay, and other models are untouched. The repository's reset marker
stays too, so a new failure of this model can still count its recent history.
'backoffs reset --quota-only' is the repository-wide reset.
"""


def _rows(data: dict[str, object], key: str) -> list[dict[str, object]]:
    value = data.get(key)
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def cmd_health(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    model = arguments.model.strip()
    where = f" from {arguments.source}" if arguments.source else ""
    if not arguments.yes and not (
        context.interactive
        and context.prompt.confirm(f"Clear {model}'s health and backoffs{where}?")
    ):
        console.line("Nothing was changed. Add --yes to reset.")
        return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {})
    try:
        if arguments.via == "github":
            console.waiting("Resetting through the model health reset workflow on GitHub")
            data = backoffs.reset_model_via_github(
                context.github, context.clock, model, arguments.source, started=context.progress
            )
            console.finish()
        else:
            data = backoffs.reset_model_direct(context.github.repo, model, arguments.source)
    except bridge.BridgeError as exc:
        return _bridge_failed(context, exc, "The model reset did not report")
    except backoffs.BackoffsUnavailableError as exc:
        raise UsageError(str(exc)) from None
    health = _rows(data, "model_health_rows")
    rows = [
        ["health", str(row.get("source")), str(row.get("status")), str(row.get("last_outcome"))]
        for row in health
    ] + [
        ["backoff", str(row.get("source")), f"streak {row.get('streak')}", str(row.get("outcome"))]
        for row in _rows(data, "backoff_rows")
    ]
    if rows:
        console.ok(f"Cleared {len(rows)} rows for {model}{where}")
        console.table(["Row", "Source", "State", "Last outcome"], rows)
    else:
        console.warn(f"Nothing was cleared for {model}{where}.")
        console.line(
            "  Use the model ID that 'baste backoffs --via github' lists, or add --source."
        )
    kept = data.get("worker_backoffs_kept")
    if isinstance(kept, int) and kept:
        console.line(f"  {kept} worker backoffs on this model were kept (baste backoffs).")
    _show_bridge(console, data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    health = commands.add_parser(
        "health",
        help="clear one model's health bench and backoffs (health reset MODEL)",
        description=MODEL_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    health.add_argument("action", choices=("reset",), help="reset")
    health.add_argument("model", help="the exact model ID, as 'baste backoffs' shows it")
    health.add_argument("--source", default="", help="only this provider source")
    health.add_argument("--yes", "-y", action="store_true", help="reset without asking")
    health.add_argument(
        "--via",
        choices=("direct", "github"),
        default="direct",
        help="github runs the repository's trusted reset workflow",
    )
    health.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    for flag in ("--json", "--verbose"):
        health.add_argument(
            flag, action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )
