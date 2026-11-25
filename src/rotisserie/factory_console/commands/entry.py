from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE
from rotisserie.factory_console.commands.agents import cmd_external, cmd_wait_for_work
from rotisserie.factory_console.commands.assign import cmd_assign, cmd_assigned
from rotisserie.factory_console.commands.backoffs import cmd_backoffs
from rotisserie.factory_console.commands.claims import (
    cmd_claim,
    cmd_finish,
    cmd_follow,
    cmd_label_event,
    cmd_labels,
    cmd_runs,
)
from rotisserie.factory_console.commands.crashes import cmd_crashes
from rotisserie.factory_console.commands.db_usage import cmd_db_usage
from rotisserie.factory_console.commands.estimate import cmd_estimate, cmd_estimate_history
from rotisserie.factory_console.commands.failures import cmd_failures
from rotisserie.factory_console.commands.fleet import cmd_models, cmd_providers
from rotisserie.factory_console.commands.heal import cmd_heal
from rotisserie.factory_console.commands.health import (
    _show_rate_limits,
    cmd_doctor,
    cmd_limits,
    cmd_outcomes,
    cmd_unstick,
)
from rotisserie.factory_console.commands.health_reset import cmd_health
from rotisserie.factory_console.commands.history import cmd_history, cmd_use
from rotisserie.factory_console.commands.incident import cmd_incident
from rotisserie.factory_console.commands.models import cmd_close, cmd_groom
from rotisserie.factory_console.commands.outcome import cmd_outcome
from rotisserie.factory_console.commands.parser import add_parser
from rotisserie.factory_console.commands.pause import cmd_pause, cmd_resume
from rotisserie.factory_console.commands.prs import (
    cmd_checks,
    cmd_conflicts,
    cmd_leases,
    cmd_review,
    cmd_sync,
)
from rotisserie.factory_console.commands.recap import cmd_recap
from rotisserie.factory_console.commands.report import cmd_report, cmd_stats
from rotisserie.factory_console.commands.run import cmd_run
from rotisserie.factory_console.commands.schema import cmd_schema
from rotisserie.factory_console.commands.self_review import cmd_allow_self_review
from rotisserie.factory_console.commands.settings import cmd_prioritize, cmd_settings
from rotisserie.factory_console.commands.status import cmd_cancel, cmd_status
from rotisserie.factory_console.commands.store import cmd_store
from rotisserie.factory_console.commands.turning import cmd_turning
from rotisserie.factory_console.commands.watch import cmd_watch
from rotisserie.factory_console.commands.why import cmd_next, cmd_why
from rotisserie.factory_console.context import Context, UsageError, find_repository
from rotisserie.factory_console.github import GitHub, GitHubError, GitHubRateLimitError
from rotisserie.factory_console.read_cache import ReadCache, pure_read
from rotisserie.factory_console.tracker import Clock
from rotisserie.factory_console.ui import Console, Prompt


def main(
    arguments: argparse.Namespace,
    *,
    env: dict[str, str] | None = None,
    stdout: TextIO | None = None,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    clock: Clock | None = None,
    executable: str = "gh",
) -> int:
    values = dict(os.environ) if env is None else env
    json_mode = bool(getattr(arguments, "json", False))
    console = Console(
        stdout or sys.stdout,
        json_mode=json_mode,
        verbose=bool(getattr(arguments, "verbose", False)),
        env=values,
        progress=(stderr or sys.stderr) if getattr(arguments, "progress", False) else None,
    )
    command = arguments.factory_command
    if command is None:
        print(_help_text(), file=console.stream)
        return EXIT_OK
    data: dict[str, object] = {"command": command}
    started = time.time()
    context: Context | None = None
    try:
        explicit = getattr(arguments, "repo", None) or getattr(arguments, "repository", None)
        repo = find_repository(explicit, values, Path.cwd())
        github = GitHub(repo, executable=executable)
        max_age = max(0, int(getattr(arguments, "max_age", 0) or 0))
        cache = ReadCache.for_repo(repo, values, max_age=max_age)
        github.use_cache(cache, reads=pure_read(arguments))
        runtime.bind_repository(repo)
        input_stream = stdin or sys.stdin
        interactive = console.interactive and input_stream.isatty() and not json_mode
        context = Context(
            github, console, Prompt(console, input_stream), clock or Clock(), interactive
        )
        data["repository"] = repo
        handler = HANDLERS[command]
        outcome = handler(context, arguments)
        data.update(outcome.data)
        data["result"] = str(outcome.result)
        code = outcome.exit_code
    except KeyboardInterrupt:
        console.finish()
        if context is not None:
            data.update(context.progress)
        data.update(result="interrupted")
        code = EXIT_INTERRUPTED
        _stopped_watching(console, data)
    except UsageError as exc:
        console.finish()
        console.fail(str(exc))
        data.update(result="invalid", error=str(exc))
        code = EXIT_USAGE
    except ValueError as exc:
        console.finish()
        console.fail(str(exc))
        data.update(result="invalid", error=str(exc))
        code = EXIT_USAGE
    except GitHubRateLimitError as exc:
        console.finish()
        console.fail(str(exc))
        if context is not None:
            data.update(_show_rate_limits(context, blocked=exc))
        data.update(result="github_unavailable", error=str(exc))
        code = exc.exit_code
    except GitHubError as exc:
        console.finish()
        console.fail(str(exc) + (f" ({exc.request})" if exc.request else ""))
        data.update(result="github_unavailable", error=str(exc))
        if exc.request:
            data["failed_request"] = exc.request
        code = exc.exit_code
    except runtime.RuntimeUnavailableError as exc:
        console.finish()
        console.fail(str(exc))
        data.update(result="invalid", error=str(exc))
        code = EXIT_USAGE
    data["exit_code"] = code
    if context is not None:
        data["read_at"] = _read_at(context.github, started)
    if json_mode:
        print(json.dumps(data, default=str, sort_keys=True), file=console.stream)
    return code


def _read_at(github: GitHub, started: float) -> str:
    """When the oldest GitHub answer behind this result was fetched."""
    oldest = github.cache.oldest if github.cache is not None else None
    moment = min(started, oldest) if oldest is not None else started
    return datetime.fromtimestamp(moment, UTC).isoformat(timespec="seconds")


def _help_text() -> str:
    return "\n".join(
        [
            "Factory operator console",
            "",
            "  baste run [PROVIDER [MODEL]] [--force] [--target issue:N]",
            "  baste status        who is working on what",
            "  baste turning       is work moving: Turning, Slowing or Burning (--follow alerts)",
            "  baste recap         morning summary: the answer first, plain words",
            "  baste report        what merged, failed and is stuck",
            "  baste failures      failed worker runs by job and step",
            "  baste stats         reliable providers and models",
            "  baste settings      limits, worker counts, priorities",
            "  baste why [issue:N] why work is or is not being picked up",
            "  baste next         ranked read-only work offers for agents",
            "  baste wait-for-work [--can ACTION]  wait until an agent can take work",
            "  baste reserve-next --client AGENT [--can ACTION] [--target pr:N]",
            "  baste renew UUID --client AGENT",
            "  baste release UUID --client AGENT",
            "  baste handoff UUID --client AGENT --evidence GITHUB_URL",
            "  baste revoke UUID --reason TEXT  remove an external agent's claim",
            "    (next, wait-for-work, reserve-next, renew, release, handoff: --via github runs",
            "     the repository's installed workflow when this machine has no claim store)",
            "  baste unstick       start the workers that clear the bottleneck",
            "  baste heal          what the factory cleared by itself",
            "  baste outcomes      did the requested work actually happen",
            "  baste outcome RUN   what one worker run did (--follow waits)",
            "  baste checks pr:N   why its checks fail",
            "  baste conflicts pr:N  what main changed under it",
            "  baste sync pr:N     merge main in (never while a worker holds it)",
            "  baste leases        who holds which item",
            "  baste backoffs      what keeps each provider waiting, and until when",
            "  baste health reset MODEL --via github  clear one model's health and backoffs",
            "  baste db-usage      Turso rows, storage and quota this month",
            "  baste store selftest  test the factory store; store export backs it up",
            "  baste schema        store tables, indexes and the last schema upgrade",
            "  baste doctor        is the factory itself healthy",
            "  baste claim pr:N    request work by label; finish pr:N ends it",
            "  baste assign pr:N --agent NAME  hand it to an outside agent; assigned lists them",
            "  baste pause --reason TEXT  drain: turn off dispatch and claims; resume undoes it",
            "  baste follow pr:N   each lifecycle change until done; runs pr:N",
            "  baste limits        GitHub API quota, reset time and the factory App budget",
            "  baste estimate issue:N  attempts, time and unattended chance",
            "  baste close pr:N --reason TEXT   close work that should not be done",
            "  baste cancel issue:N",
            "  baste watch RUN",
            "  baste providers | models PROVIDER | prioritize issue:N",
            "  baste providers disable|enable NAME [--reason TEXT]",
            "  baste models disable|enable PROVIDER MODEL [--reason TEXT]",
            "  baste incident [start issue:N [--attempts K] | stop issue:N]",
            "",
            "Run 'baste COMMAND --help' for details.",
        ]
    )


def _stopped_watching(console: Console, data: dict[str, object]) -> None:
    console.line()
    console.warn("Stopped watching.")
    console.line("  Your request keeps running on GitHub. Nothing was cancelled.")
    run = data.get("scheduler_run_id") or data.get("worker_run_id")
    if run:
        console.line(f"  Resume:  baste watch {run}")
    bridged = data.get("bridge_run")
    if bridged:
        # A claim run may still reserve or release work: look before retrying.
        console.line(f"  Its result: {bridged}")
    target = data.get("target_ref")
    if target:
        console.line(f"  Stop it: baste cancel {target}")


HANDLERS = {
    "run": cmd_run,
    "watch": cmd_watch,
    "status": cmd_status,
    "who": cmd_status,
    "cancel": cmd_cancel,
    "report": cmd_report,
    "failures": cmd_failures,
    "stats": cmd_stats,
    "settings": cmd_settings,
    "prioritize": cmd_prioritize,
    "why": cmd_why,
    "next": cmd_next,
    "wait-for-work": cmd_wait_for_work,
    "reserve-next": cmd_external,
    "renew": cmd_external,
    "release": cmd_external,
    "handoff": cmd_external,
    "revoke": cmd_external,
    "unstick": cmd_unstick,
    "outcomes": cmd_outcomes,
    "close": cmd_close,
    "checks": cmd_checks,
    "review": cmd_review,
    "allow-self-review": cmd_allow_self_review,
    "groom": cmd_groom,
    "conflicts": cmd_conflicts,
    "sync": cmd_sync,
    "leases": cmd_leases,
    "doctor": cmd_doctor,
    "limits": cmd_limits,
    "labels": cmd_labels,
    "claim": cmd_claim,
    "finish": cmd_finish,
    "runs": cmd_runs,
    "history": cmd_history,
    "estimate": cmd_estimate,
    "estimate-history": cmd_estimate_history,
    "use": cmd_use,
    "follow": cmd_follow,
    "label-event": cmd_label_event,
    "explain": cmd_why,
    "providers": cmd_providers,
    "models": cmd_models,
    "incident": cmd_incident,
    "backoffs": cmd_backoffs,
    "db-usage": cmd_db_usage,
    "crashes": cmd_crashes,
    "schema": cmd_schema,
    "recap": cmd_recap,
    "heal": cmd_heal,
    "store": cmd_store,
    "turning": cmd_turning,
    "assign": cmd_assign,
    "assigned": cmd_assigned,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "health": cmd_health,
    "outcome": cmd_outcome,
}


def run_cli(argv: Sequence[str]) -> int:
    """Standalone entry for tests: parse ``factory ...`` arguments and run."""
    parser = argparse.ArgumentParser(prog="rotisserie")
    sub = parser.add_subparsers(dest="command")
    add_parser(sub)
    return main(parser.parse_args(list(argv)))
