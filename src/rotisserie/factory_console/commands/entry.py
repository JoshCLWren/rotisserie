from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE
from rotisserie.factory_console.commands.agents import cmd_external, cmd_wait_for_work
from rotisserie.factory_console.commands.backoffs import cmd_backoffs
from rotisserie.factory_console.commands.claims import (
    cmd_claim,
    cmd_finish,
    cmd_follow,
    cmd_label_event,
    cmd_labels,
    cmd_runs,
)
from rotisserie.factory_console.commands.estimate import cmd_estimate, cmd_estimate_history
from rotisserie.factory_console.commands.failures import cmd_failures
from rotisserie.factory_console.commands.fleet import cmd_models, cmd_providers
from rotisserie.factory_console.commands.health import (
    _show_rate_limits,
    cmd_doctor,
    cmd_limits,
    cmd_outcomes,
    cmd_unstick,
)
from rotisserie.factory_console.commands.history import cmd_history, cmd_use
from rotisserie.factory_console.commands.incident import cmd_incident
from rotisserie.factory_console.commands.models import cmd_close, cmd_groom
from rotisserie.factory_console.commands.parser import add_parser
from rotisserie.factory_console.commands.prs import (
    cmd_checks,
    cmd_conflicts,
    cmd_leases,
    cmd_review,
    cmd_sync,
)
from rotisserie.factory_console.commands.report import cmd_report, cmd_stats
from rotisserie.factory_console.commands.run import cmd_run
from rotisserie.factory_console.commands.settings import cmd_prioritize, cmd_settings
from rotisserie.factory_console.commands.status import cmd_cancel, cmd_status
from rotisserie.factory_console.commands.watch import cmd_watch
from rotisserie.factory_console.commands.why import cmd_next, cmd_why
from rotisserie.factory_console.context import Context, UsageError, find_repository
from rotisserie.factory_console.github import GitHub, GitHubError, GitHubRateLimitError
from rotisserie.factory_console.tracker import Clock
from rotisserie.factory_console.ui import Console, Prompt


def main(
    arguments: argparse.Namespace,
    *,
    env: dict[str, str] | None = None,
    stdout: TextIO | None = None,
    stdin: TextIO | None = None,
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
    )
    command = arguments.factory_command
    if command is None:
        print(_help_text(), file=console.stream)
        return EXIT_OK
    data: dict[str, object] = {"command": command}
    context: Context | None = None
    try:
        explicit = getattr(arguments, "repo", None) or getattr(arguments, "repository", None)
        repo = find_repository(explicit, values, Path.cwd())
        github = GitHub(repo, executable=executable)
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
    if json_mode:
        print(json.dumps(data, default=str, sort_keys=True), file=console.stream)
    return code


def _help_text() -> str:
    return "\n".join(
        [
            "Factory operator console",
            "",
            "  rotisserie factory run [PROVIDER [MODEL]] [--force] [--target issue:N]",
            "  rotisserie factory status        who is working on what",
            "  rotisserie factory report        what merged, failed and is stuck",
            "  rotisserie factory failures      failed worker runs by job and step",
            "  rotisserie factory stats         reliable providers and models",
            "  rotisserie factory settings      limits, worker counts, priorities",
            "  rotisserie factory why [issue:N] why work is or is not being picked up",
            "  rotisserie factory next         ranked read-only work offers for agents",
            "  rotisserie factory wait-for-work [--can ACTION]  wait until an agent can take work",
            "  rotisserie factory reserve-next --client AGENT [--can ACTION] [--target pr:N]",
            "  rotisserie factory renew UUID --client AGENT",
            "  rotisserie factory release UUID --client AGENT",
            "  rotisserie factory handoff UUID --client AGENT --evidence GITHUB_URL",
            "  rotisserie factory revoke UUID --reason TEXT  remove an external agent's claim",
            "    (next, wait-for-work, reserve-next, renew, release, handoff: --via github runs",
            "     the repository's installed workflow when this machine has no claim store)",
            "  rotisserie factory unstick       start the workers that clear the bottleneck",
            "  rotisserie factory outcomes      did the requested work actually happen",
            "  rotisserie factory checks pr:N   why its checks fail",
            "  rotisserie factory conflicts pr:N  what main changed under it",
            "  rotisserie factory sync pr:N     merge main in (never while a worker holds it)",
            "  rotisserie factory leases        who holds which item",
            "  rotisserie factory backoffs      what keeps each provider waiting, and until when",
            "  rotisserie factory doctor        is the factory itself healthy",
            "  rotisserie factory claim pr:N    request work by label; finish pr:N ends it",
            "  rotisserie factory follow pr:N   each lifecycle change until done; runs pr:N",
            "  rotisserie factory limits        GitHub API quota and reset time",
            "  rotisserie factory estimate issue:N  attempts, time and unattended chance",
            "  rotisserie factory close pr:N --reason TEXT   close work that should not be done",
            "  rotisserie factory cancel issue:N",
            "  rotisserie factory watch RUN",
            "  rotisserie factory providers | models PROVIDER | prioritize issue:N",
            "  rotisserie factory providers disable|enable NAME [--reason TEXT]",
            "  rotisserie factory models disable|enable PROVIDER MODEL [--reason TEXT]",
            "  rotisserie factory incident [start issue:N [--attempts K] | stop issue:N]",
            "",
            "Run 'rotisserie factory COMMAND --help' for details.",
        ]
    )


def _stopped_watching(console: Console, data: dict[str, object]) -> None:
    console.line()
    console.warn("Stopped watching.")
    console.line("  Your request keeps running on GitHub. Nothing was cancelled.")
    run = data.get("scheduler_run_id") or data.get("worker_run_id")
    if run:
        console.line(f"  Resume:  rotisserie factory watch {run}")
    bridged = data.get("bridge_run")
    if bridged:
        # A claim run may still reserve or release work: look before retrying.
        console.line(f"  Its result: {bridged}")
    target = data.get("target_ref")
    if target:
        console.line(f"  Stop it: rotisserie factory cancel {target}")


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
}


def run_cli(argv: Sequence[str]) -> int:
    """Standalone entry for tests: parse ``factory ...`` arguments and run."""
    parser = argparse.ArgumentParser(prog="rotisserie")
    sub = parser.add_subparsers(dest="command")
    add_parser(sub)
    return main(parser.parse_args(list(argv)))
