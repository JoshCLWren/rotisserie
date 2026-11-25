from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timedelta

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.parser_insight import _common
from rotisserie.factory_console.commands.status import parse_since
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import turning
from rotisserie.factory_console.tracker import Result
from rotisserie.factory_console.ui import Console, local_time, since

EXIT_BURNING = 10
EXIT_SLOWING = 11
ALERT_EXITS = {turning.BURNING: EXIT_BURNING, turning.SLOWING: EXIT_SLOWING}
FACES = {
    turning.TURNING: "🍗🔄 Turning",
    turning.SLOWING: "🐢 Slowing",
    turning.BURNING: "🔥🔥 Burning",
    turning.UNKNOWN: "❔ Unknown",
}
HELP = """\
Is the factory actually moving work right now? 🍗🔄 Turning, 🐢 Slowing or 🔥🔥 Burning.

  baste turning                    one verdict, then the evidence
  baste turning --follow           check every 5 minutes; exit on Burning or Slowing
  baste turning --follow --notify  also show a macOS notification

Burning: no worker started for longer than --idle (45m) while eligible work or
workers exist. Slowing: --failures (3) or more control workflow runs failed in
--window (1h), or an item loops: 4 or more worker runs on it in 6h, at least
half of them wasted, and it is still short of factory:ready. Unknown: the
worker runs could not be read. Each verdict names its most likely reason and
the command that digs further.

--follow prints only changes. It exits 10 on Burning and 11 on Slowing, the
first reading included, so scripts and agents can wake on it.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "turning",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--idle", default="45m", help="Burning after no worker start this long")
    parser.add_argument("--window", default="1h", help="how far back to count control failures")
    parser.add_argument("--failures", type=int, default=turning.FAILURES, help="Slowing at N")
    parser.add_argument("--passes", type=int, default=turning.PASSES, help="dispatcher passes (3)")
    parser.add_argument("--follow", action="store_true", help="keep checking; exit on an alert")
    parser.add_argument("--every", default="5m", help="with --follow: time between checks")
    parser.add_argument("--notify", action="store_true", help="with --follow: macOS notification")
    _common(parser)


def _span(value: str, flag: str, now: datetime) -> timedelta:
    try:
        span = now - parse_since(value, now)
    except UsageError:
        raise UsageError(f"{flag} takes 45m, 2h or 1d") from None
    if span <= timedelta(0):
        raise UsageError(f"{flag} must be longer than zero")
    return span


def _when(value: object) -> str:
    moment = turning.moment(str(value or ""))
    return local_time(moment) if moment else "unknown"


def _ago(value: object, now: datetime) -> str:
    moment = turning.moment(str(value or ""))
    return f"{local_time(moment)} ({since(moment, now)} ago)" if moment else "never"


def decorate(text: str) -> str:
    """Put the verdict's emoji before a plain 'Burning: ...' doctor or recap line."""
    word, _, rest = text.partition(":")
    face = FACES.get(word.strip().lower())
    return f"{face}:{rest}" if face else text


def render_head(console: Console, data: dict[str, object]) -> None:
    console.line(console.bold(FACES.get(str(data["verdict"]), str(data["verdict"]))))
    if data.get("text"):
        console.line(f"  {data['text']}")
    if data.get("next"):
        console.line(f"  👉 Next: {data['next']}")


def _reason_word(name: str) -> str:
    return "reason not logged" if name == "not logged" else name


def _passes(console: Console, rows: object) -> None:
    passes = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    console.line(f"🎛️ Dispatcher passes ({len(passes)} newest)")
    for row in passes:
        reasons = row.get("none_reasons") or {}
        why = ", ".join(f"{_reason_word(k)} {v}" for k, v in reasons.items()) if reasons else ""
        line = (
            f"    {_when(row.get('at'))}  {row.get('eligible', '?')} eligible, "
            f"{row.get('assigned')} assigned, {row.get('none')} none"
        )
        console.line(line + (f" ({why})" if why else "") + f"  {row.get('url', '')}")


def render(console: Console, data: dict[str, object], now: datetime) -> None:
    render_head(console, data)
    ev = data.get("evidence")
    if not isinstance(ev, dict):
        return
    console.line()
    running = ev.get("workers_running")
    console.line(
        f"🧑‍🍳 Last worker started: {_ago(ev.get('last_worker_started'), now)}"
        + (f", {running} running now" if running else "")
    )
    console.line(f"🍽️ Last outcome served: {_ago(ev.get('last_outcome_recorded'), now)}")
    _passes(console, ev.get("dispatcher_passes"))
    console.line(f"🎛️ Dispatcher last ran: {_ago(ev.get('dispatcher_last_ran'), now)}")
    failures = ev.get("control_failures") or []
    console.line(
        f"💥 Control failures in the last {ev.get('failure_window_minutes')}m: {len(failures)} "
        f"(Slowing at {ev.get('failure_threshold')})"
    )
    for row in failures[:6] if isinstance(failures, list) else []:
        step = f" at '{row['step']}'" if row.get("step") else ""
        console.line(f"    {row['workflow']}{step}  {_when(row.get('at'))}  {row.get('url')}")
    _render_loops(console, ev.get("item_loops"))
    _render_capacity(console, ev)


def _render_loops(console: Console, rows: object) -> None:
    found = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    console.line(f"🔁 Looping items: {len(found)}")
    for row in found[:5]:
        name = f"{row.get('title') or row.get('target')} (#{row.get('number')})"
        console.line(f"    {name}: {row.get('text')}  {row.get('url') or ''}".rstrip())
        console.line(f"      👉 {row.get('next')}")
        for run in (row.get("runs") or [])[-6:]:
            kind = run.get("kind") or "?"
            console.line(
                f"      {_when(run.get('at'))}  {kind}  {run.get('model')}  {run.get('outcome')}"
            )


def _render_capacity(console: Console, ev: dict[str, object]) -> None:
    providers = _dict(ev.get("providers"))
    if providers.get("read"):
        cooling = providers.get("cooling") or []
        names = ", ".join(str(n) for n in cooling) if isinstance(cooling, list) else ""
        console.line(
            f"🧊 Providers cooling: {len(cooling) if isinstance(cooling, list) else 0} "
            f"of {providers.get('sources')}" + (f" ({names})" if names else "")
        )
    passes = ev.get("dispatcher_passes")
    first = passes[0] if isinstance(passes, list) and passes else {}
    filtered = first.get("filtered") if isinstance(first, dict) else None
    if isinstance(filtered, dict) and filtered:
        console.line("🧊 Capped or off on the last pass: " + ", ".join(sorted(filtered)))
    backlog = _dict(ev.get("backlog"))
    if backlog:
        line = (
            f"📦 Backlog: {backlog.get('eligible_issues')} eligible issues, "
            f"{backlog.get('open_prs')} open factory PRs"
        )
        console.line(line + (f". {backlog['limit_text']}" if backlog.get("limit_text") else ""))
    holds = _dict(ev.get("holds"))
    if holds:
        expired = holds.get("expired")
        text = "not readable here: " + str(holds.get("note")) if expired is None else expired
        console.line(f"⏰ Expired holds: {text}")
    store = _dict(ev.get("store"))
    if store:
        step = store.get("dispatcher_store_step") or "unknown"
        console.line(
            f"🗄️ Store: last dispatcher pass {step}; db usage {store.get('db_usage_status')}, "
            f"published {_when(store.get('db_usage_checked_at'))}"
            + ("; Turso reports BLOCKED" if store.get("blocked") else "")
        )
    unread = ev.get("unread")
    if unread:
        console.warn(
            "Not read: " + ", ".join(str(u) for u in unread) if isinstance(unread, list) else ""
        )


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def notify(text: str) -> None:
    if shutil.which("osascript") is None:
        return
    script = f'display notification {json.dumps(text, ensure_ascii=False)} with title "Rotisserie"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10, capture_output=True)
    except OSError, subprocess.SubprocessError:
        return


def cmd_turning(context: Context, arguments: argparse.Namespace) -> Outcome:
    now = context.clock.now()
    idle = _span(arguments.idle, "--idle", now)
    window = _span(arguments.window, "--window", now)
    every = _span(arguments.every, "--every", now)
    if not 1 <= arguments.passes <= 6:
        raise UsageError("--passes must be between 1 and 6")
    if arguments.failures < 1:
        raise UsageError("--failures must be at least 1")
    console = context.console

    def check() -> dict[str, object]:
        found = turning.gather(
            context.github,
            now=context.clock.now(),
            clock=context.clock,
            idle=idle,
            window=window,
            threshold=arguments.failures,
            passes=arguments.passes,
        )
        return turning.assess(found).to_dict()

    if not arguments.follow:
        console.waiting("Checking whether the factory is turning")
        data = check()
        console.finish()
        render(console, data, context.clock.now())
        return Outcome(Result.COMPLETED, EXIT_OK, data)
    changes: list[dict[str, object]] = []
    last: tuple[object, object] | None = None
    while True:
        data = check()
        moment = context.clock.now()
        key = (data["verdict"], data["reason"])
        if key != last:
            changes.append({"at": moment.isoformat(), "verdict": data["verdict"]})
            console.line(console.dim(local_time(moment)))
            if last is None:
                render(console, data, moment)
            else:
                render_head(console, data)
            last = key
        code = ALERT_EXITS.get(str(data["verdict"]))
        if code is not None:
            face = FACES[str(data["verdict"])]
            alert = f"🚨🔥 ALERT {local_time(moment)}: {face}. {data['text']}"
            console.line(console.bold(alert))
            if arguments.notify:
                notify(f"{face}: {data['text']}")
            return Outcome(Result.COMPLETED, code, {**data, "changes": changes, "alert": True})
        context.clock.sleep(every.total_seconds())
