from __future__ import annotations

import argparse
from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import (
    EXIT_OK,
    PRIORITY_LABELS,
    Outcome,
    _split_target,
)
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import GitHubNotFoundError, Obj, timestamp
from rotisserie.factory_console.service import providers, reliability
from rotisserie.factory_console.tracker import Result
from rotisserie.factory_console.ui import duration, local_time
from rotisserie.factory_console.work import lookup


def _health_word(health: Obj, now: datetime) -> str:
    status = reliability.health_status(health, now)
    if status == "paused":
        until = timestamp(health, "cooldown_until")
        return f"paused until {local_time(until)}" if until else "paused"
    return {"healthy": "ok", "retired": "retired", "unknown": ""}.get(status, status)


def cmd_settings(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    change: list[str] = arguments.change
    known = {setting.name: setting for setting in runtime.settings()}
    if change:
        name = _setting_name(change[0], known)
        setting = known[name]
        if arguments.reset:
            try:
                context.github.gh("variable", "delete", name, "--repo", context.repo)
            except GitHubNotFoundError:
                pass
            console.ok(
                f"{setting.title} reset to the default ({_format(setting.unit, setting.default)})"
            )
            return Outcome(
                Result.COMPLETED,
                EXIT_OK,
                {"name": name, "value": setting.default, "origin": "default"},
            )
        if len(change) != 2:
            raise UsageError(f"Give a value: rotisserie factory settings {name} VALUE")
        value = runtime.validate_setting(name, change[1])
        context.github.gh("variable", "set", name, "--repo", context.repo, "--body", value)
        console.ok(
            f"{setting.title} set to {_format(setting.unit, value)}",
            "applies to the next factory run",
        )
        return Outcome(
            Result.COMPLETED,
            EXIT_OK,
            {"name": name, "value": value, "origin": "repository variable"},
        )
    configured = _variables(context.github)
    console.heading("Factory settings")
    rows = []
    out = []
    group = ""
    for setting in runtime.settings():
        if setting.group != group:
            group = setting.group
        current = configured.get(setting.name)
        value_text = current if current is not None else setting.default
        shown = _format(setting.unit, value_text)
        rows.append(
            [setting.title, shown, "set" if current is not None else "default", setting.name]
        )
        out.append(
            {
                "name": setting.name,
                "title": setting.title,
                "value": value_text,
                "origin": "repository variable" if current is not None else "default",
                "description": setting.description,
            }
        )
    console.table(["Setting", "Value", "", "Name"], rows)
    console.line()
    console.note("Change one: rotisserie factory settings NAME VALUE   (or NAME --reset)")
    console.note("Forced runs (--force) ignore these limits.")
    if console.verbose:
        for setting in runtime.settings():
            console.line(f"  {setting.name}: {setting.description}")
    return Outcome(Result.COMPLETED, EXIT_OK, {"settings": out})


def _setting_name(value: str, known: dict[str, runtime.SettingInfo]) -> str:
    candidate = value.upper().replace("-", "_")
    for name in (candidate, f"FACTORY_{candidate}"):
        if name in known:
            return name
    raise UsageError(f"Unknown setting {value}. See: rotisserie factory settings")


def _format(unit: str, value: str) -> str:
    if unit == "seconds" and value.isdecimal():
        return duration(int(value))
    if unit == "order":
        return " > ".join(value.split(","))
    return value


_variables = providers.repository_variables


def cmd_prioritize(context: Context, arguments: argparse.Namespace) -> Outcome:
    kind, number = _split_target(arguments.what)
    current = lookup(context.github, kind, number)
    remove = [label for label in PRIORITY_LABELS if label in current.labels]
    for label in remove:
        context.github.gh(
            "issue", "edit", str(number), "--repo", context.repo, "--remove-label", label
        )
    if arguments.level != "none":
        label = f"ralph-priority:{arguments.level}"
        context.github.gh(
            "label",
            "create",
            label,
            "--repo",
            context.repo,
            "--force",
            "--color",
            "b60205",
            "--description",
            "Factory pick-up priority",
        )
        context.github.gh(
            "issue", "edit", str(number), "--repo", context.repo, "--add-label", label
        )
        context.console.ok(
            f"#{number} priority set to {arguments.level}",
            "picked ahead of lower priorities in its category",
        )
    else:
        context.console.ok(f"#{number} priority cleared")
    context.console.note(
        "Category order (main breakage, user bugs, …) is the FACTORY_PRIORITY_ORDER setting."
    )
    return Outcome(Result.COMPLETED, EXIT_OK, {"ref": arguments.what, "level": arguments.level})
