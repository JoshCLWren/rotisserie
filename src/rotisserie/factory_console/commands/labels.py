from __future__ import annotations

import argparse
import re

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.claims import cmd_label_doctor
from rotisserie.factory_console.commands.parser_insight import _common, _via
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import label_explain, label_glossary
from rotisserie.factory_console.tracker import Result

HELP = """\
What each factory label means, for every label or for one issue or PR.

  baste labels                  the glossary: every factory label and state
  baste labels 5963             each label on #5963 and what it means there
  baste labels --audit          label doctor: audit labels against the store (dry run)
  baste labels --repair         label doctor: apply the planned label changes

The item form reads the item's labels from GitHub. A label with no glossary
entry is "not a factory label". The last line says what the labels show the
factory does next. Labels mirror the store, so 'baste why issue:N' gives the
full eligibility.
"""
ITEM = re.compile(r"^(?:(?:issue|pr):)?#?([1-9][0-9]*)$")


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    tags = commands.add_parser(
        "labels",
        help="what each factory label means; --audit or --repair runs the label doctor",
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    tags.add_argument("item", nargs="?", help="issue or PR number (N, issue:N or pr:N)")
    tags.add_argument(
        "--audit", action="store_true", help="audit labels against the store (dry run)"
    )
    tags.add_argument("--repair", action="store_true", help="apply the planned label changes")
    tags.add_argument(
        "--delete-legacy",
        action="store_true",
        help="with --repair, also delete the obsolete label definitions it lists",
    )
    tags.add_argument(
        "--no-wait", action="store_true", help="with --via github, start the run and return"
    )
    tags.add_argument("--run", type=int, help="show the report of an earlier doctor run")
    _via(tags)
    _common(tags)


def _doctor(arguments: argparse.Namespace) -> bool:
    flags = (arguments.audit, arguments.repair, arguments.delete_legacy, arguments.no_wait)
    return any(flags) or bool(arguments.run) or arguments.via == "github"


def cmd_labels(context: Context, arguments: argparse.Namespace) -> Outcome:
    if _doctor(arguments):
        if arguments.item:
            raise UsageError("Give an item number or a label doctor flag, not both")
        return cmd_label_doctor(context, arguments)
    if arguments.item is None:
        data = label_glossary.glossary()
        _show_glossary(context, data)
        return Outcome(Result.COMPLETED, EXIT_OK, data)
    found = ITEM.fullmatch(str(arguments.item).strip())
    if found is None:
        raise UsageError(f"Not an issue or PR number: {arguments.item}")
    data = label_explain.explain(context.github, int(found.group(1)))
    _show_item(context, data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _show_glossary(context: Context, data: dict[str, object]) -> None:
    console = context.console
    entries = data.get("entries")
    group = None
    for entry in entries if isinstance(entries, list) else []:
        if entry["group"] != group:
            group = entry["group"]
            console.heading(f"{str(group).capitalize()} labels")
        console.line(f"  {entry['name']}: {entry['meaning']}")
        console.line(f"      set by: {entry['set_by']}")
        console.line(f"      cleared by: {entry['cleared_by']}")
        console.line(f"      effect: {entry['effect']}")
    legacy = data.get("legacy")
    if isinstance(legacy, dict):
        console.heading("Legacy labels")
        console.line(f"  {legacy['meaning']} {legacy['cleared_by']}")
        console.line(f"  {', '.join(map(str, legacy.get('labels') or []))}")


def _show_item(context: Context, data: dict[str, object]) -> None:
    console = context.console
    console.heading(f"Labels on {data['target']} ({data['state']}): {data.get('title') or ''}")
    rows = data.get("labels")
    if not rows:
        console.line("  No labels.")
    for row in rows if isinstance(rows, list) else []:
        if row["factory"]:
            console.line(f"  {row['label']}: {row['meaning']} {row['effect']}")
        else:
            console.line(f"  {row['label']}: not a factory label")
    step = data.get("next")
    if step:
        console.line(f"Next: {step}")
    else:
        target = str(data["target"])
        hint = f"baste why {target}"
        if not target.startswith("issue:"):
            hint = f"baste follow {target} --once"
        console.line(f"Next: not clear from the labels; run {hint}")
