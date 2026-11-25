from __future__ import annotations

import argparse

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import timestamp
from rotisserie.factory_console.service import pause
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.ui import local_time


def describe(found: dict[str, object]) -> str:
    since = timestamp({"at": str(found.get("since") or "")}, "at")
    reason = str(found.get("reason") or "no reason given")
    return f"paused since {local_time(since)}: {reason}" if since else f"paused: {reason}"


def show_pause(context: Context, data: dict[str, object]) -> None:
    """The pause banner that status, turning and recap print first."""
    found = pause.state(context.github)
    data["paused"] = found
    if found is not None:
        context.console.warn(f"The factory is {describe(found)}")
        context.console.line("  Running work continues. Turn it back on: baste resume")


def paused_refusal(context: Context, what: str) -> Outcome | None:
    found = pause.record(context.github)
    if found is None:
        return None
    context.console.warn(f"The factory is {describe(found)}")
    context.console.line(f"  {what}: the claim controller is off until 'baste resume'.")
    return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {"paused": found})


def _confirmed(context: Context, arguments: argparse.Namespace, question: str) -> bool:
    if arguments.yes:
        return True
    return context.interactive and context.prompt.confirm(question)


def _names(data: dict[str, object], key: str) -> list[str]:
    value = data.get(key)
    return [str(item) for item in value] if isinstance(value, list) else []


def _errors(data: dict[str, object]) -> dict[str, str]:
    value = data.get("errors")
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _credentials(exc: pause.PauseCredentialError) -> UsageError:
    return UsageError(str(exc))


def cmd_pause(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    names = ", ".join(pause.WORKFLOWS.values())
    if not _confirmed(context, arguments, f"Turn off {names}?"):
        console.line("Nothing was changed. Add --yes to pause.")
        return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {})
    try:
        data = pause.pause(context.github, reason=arguments.reason, now=context.clock.now())
    except pause.PauseCredentialError as exc:
        raise _credentials(exc) from None
    for file in _names(data, "turned_off"):
        console.ok(f"Turned off {pause.WORKFLOWS[file]}", file)
    for file in _names(data, "left_off"):
        console.line(f"  {file} was already off; resume leaves it off")
    for file in _names(data, "missing"):
        console.line(f"  {file} is not installed here")
    for file, error in _errors(data).items():
        console.fail(f"Could not turn off {file}", error)
    console.line(f"The factory is {describe(data)}. Running work was not cancelled.")
    console.line("Turn it back on: baste resume")
    good = data["status"] == "paused"
    return Outcome(Result.COMPLETED if good else Result.FAILED, EXIT_OK if good else 4, data)


def cmd_resume(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    if not _confirmed(context, arguments, "Turn the paused factory workflows back on?"):
        console.line("Nothing was changed. Add --yes to resume.")
        return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {})
    try:
        data = pause.resume(context.github)
    except pause.PauseCredentialError as exc:
        raise _credentials(exc) from None
    if not data["recorded"]:
        console.warn("No pause record was found; turning on the factory workflows that are off")
    turned_on = _names(data, "turned_on")
    for file in turned_on:
        console.ok(f"Turned on {pause.WORKFLOWS.get(file, file)}", file)
    if not turned_on:
        console.line("  No factory workflow needed turning on.")
    for file, error in _errors(data).items():
        console.fail(f"Could not turn on {file}", error)
    good = data["status"] == "resumed"
    return Outcome(Result.COMPLETED if good else Result.FAILED, EXIT_OK if good else 4, data)
