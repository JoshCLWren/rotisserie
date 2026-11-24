from __future__ import annotations

import argparse
from collections import Counter

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome, discover_models
from rotisserie.factory_console.commands.report import _count, _load_stats, _provider_rows, _window
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import items, string
from rotisserie.factory_console.service import fleet, providers
from rotisserie.factory_console.tracker import Result
from rotisserie.factory_console.work import current_work

SWITCH_WORDS = ("disable", "enable")


def _reason(off: fleet.Off | None) -> str:
    if off is None:
        return ""
    return "off" + (f": {off.reason}" if off.reason else "")


def _off_reason(off: fleet.Off | None) -> str | None:
    return (off.reason or None) if off else None


def _switch_words(words: list[str], count: int, usage: str) -> tuple[bool, list[str]] | None:
    if not words or words[0] not in SWITCH_WORDS:
        return None
    if len(words) != count:
        raise UsageError(f"Usage: {usage}")
    return words[0] == "enable", words[1:]


def _report(context: Context, change: fleet.Change, label: str) -> Outcome:
    console = context.console
    if not change.changed:
        console.ok(f"{label} was already {'on' if change.enabled else 'off'}")
    elif change.enabled:
        console.ok(f"{label} turned on", "the next dispatcher tick may use it again")
    else:
        console.ok(
            f"{label} turned off",
            "the next dispatcher tick skips it; running work continues",
        )
        console.note("Stop running work if needed: rotisserie factory cancel issue:N (or pr:N)")
    return Outcome(Result.COMPLETED, EXIT_OK, change.to_dict())


def cmd_providers(context: Context, arguments: argparse.Namespace) -> Outcome:
    usage = "rotisserie factory providers disable|enable NAME [--reason TEXT]"
    switch = _switch_words(arguments.change, 2, usage)
    if switch is not None:
        enabled, (name,) = switch
        try:
            change = fleet.set_provider(
                context.github,
                name,
                enabled=enabled,
                reason=arguments.reason,
                now=context.clock.now(),
            )
        except fleet.SwitchError as exc:
            raise UsageError(str(exc)) from None
        return _report(context, change, f"Provider {fleet.display_name(change.provider)}")
    if arguments.change:
        raise UsageError(f"Usage: {usage}")
    console = context.console
    working = Counter(item.provider for item in current_work(context.github, with_session=False))
    stats = _load_stats(context.github)
    delivered: dict[str, str] = {}
    if stats is not None:
        for row in _provider_rows(stats, "7d"):
            delivered[row[0]] = f"{row[2]} PRs / {row[1]} runs"
    variables = providers.repository_variables(context.github)
    switched = fleet.state(variables)
    rows = []
    out = []
    for setup in providers.provider_setup(context.github, variables):
        provider = setup.provider
        ready = (
            "unknown"
            if not setup.known
            else ("ready" if not setup.missing else "missing " + ", ".join(setup.missing))
        )
        off = switched.provider_off(provider.source)
        rows.append(
            [
                provider.key,
                provider.name,
                ready,
                _reason(off) or "on",
                str(working.get(provider.name, 0)),
                delivered.get(provider.name, ""),
            ]
        )
        out.append(
            {
                "provider": provider.key,
                "name": provider.name,
                "setup": ready,
                "disabled": off is not None,
                "disabled_reason": off.reason if off else None,
                "working_now": working.get(provider.name, 0),
                "model_prefix": provider.model_prefix or None,
            }
        )
    for name, title in sorted(fleet.extra_lanes().items()):
        off = switched.provider_off(name)
        rows.append([name, title, "outside the worker fleet", _reason(off) or "on", "", ""])
        out.append(
            {
                "provider": name,
                "name": title,
                "setup": "outside the worker fleet",
                "disabled": off is not None,
                "disabled_reason": off.reason if off else None,
                "working_now": None,
                "model_prefix": None,
            }
        )
    console.heading("Providers")
    console.table(["Provider", "Name", "Setup", "Switch", "Working", "Last 7 days"], rows)
    console.line()
    console.note(
        "A ready provider still has to prove itself: "
        "the worker checks price, quota and access at start."
    )
    console.note("Turn one off: rotisserie factory providers disable NAME --reason TEXT")
    return Outcome(
        Result.COMPLETED,
        EXIT_OK,
        {"providers": out, "disabled_models": [m.to_dict() for m in switched.models.values()]},
    )


def cmd_models(context: Context, arguments: argparse.Namespace) -> Outcome:
    usage = "rotisserie factory models disable|enable PROVIDER MODEL [--reason TEXT]"
    switch = _switch_words(arguments.words, 3, usage)
    if switch is not None:
        enabled, (provider_name, model) = switch
        try:
            change = fleet.set_model(
                context.github,
                provider_name,
                model,
                enabled=enabled,
                reason=arguments.reason,
                now=context.clock.now(),
            )
        except fleet.SwitchError as exc:
            raise UsageError(str(exc)) from None
        return _report(context, change, f"Model {change.model}")
    if len(arguments.words) != 1:
        raise UsageError(f"Usage: rotisserie factory models PROVIDER, or {usage}")
    return _list_models(context, arguments.words[0])


def _list_models(context: Context, provider_name: str) -> Outcome:
    console = context.console
    provider = runtime.resolve_provider(provider_name)
    stats = _load_stats(context.github)
    record: dict[str, str] = {}
    if stats is not None:
        for entry in items(stats, "models"):
            if string(entry, "source") == provider.source:
                counts = _window(entry, "7d")
                model = string(entry, "model")
                full = (
                    model
                    if model.startswith(provider.model_prefix)
                    else provider.model_prefix + model
                )
                record[full] = f"{_count(counts, 'delivered')} PRs / {_count(counts, 'runs')} runs"
    models = discover_models(provider)
    known = {model for model, _ in models}
    rows = [[model, record.get(model, ""), origin] for model, origin in models]
    # A model with a track record that the live lists no longer include would be
    # refused by the worker; say so instead of offering it.
    gone = "no longer offered (the worker would refuse it)" if known else "ran in the factory"
    rows += [[model, value, gone] for model, value in record.items() if model not in known]
    switched = fleet.read(context.github)
    listed = {row[0] for row in rows}
    rows += [
        [model, "", "turned off"]
        for (source, model) in switched.models
        if source == provider.source and model not in listed
    ]
    rows.sort(key=lambda row: (row[1] == "", row[0]))
    shown = [[*row, _reason(switched.model_off(provider.source, row[0])) or "on"] for row in rows]
    console.heading(f"{provider.name} models")
    off = switched.provider_off(provider.source)
    if off is not None:
        console.warn(f"{provider.key} is turned off{f': {off.reason}' if off.reason else ''}")
    if not provider.model_prefix:
        console.line("This provider picks its model automatically; use 'auto'.")
    elif not rows:
        console.line("No models discovered. You can still pass an exact model ID.")
    console.table(["Model", "Last 7 days", "Source", "Switch"], shown)
    console.line()
    console.note(
        "A listed model is not proof it will run: "
        "the worker checks price and access when it starts."
    )
    return Outcome(
        Result.COMPLETED,
        EXIT_OK,
        {
            "provider": provider.key,
            "provider_disabled": off is not None,
            "models": [
                {
                    "model": r[0],
                    "track_record": r[1] or None,
                    "source": r[2],
                    "offered": (r[0] in known) if known else None,
                    "disabled": switched.model_off(provider.source, r[0]) is not None,
                    "disabled_reason": _off_reason(switched.model_off(provider.source, r[0])),
                }
                for r in rows
            ],
        },
    )
