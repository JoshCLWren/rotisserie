from __future__ import annotations

import argparse
from pathlib import Path

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.why import _bridge_failed, _show_bridge
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import GitHubError
from rotisserie.factory_console.service import bridge, store
from rotisserie.factory_console.tracker import EXIT_CODES, Result

HELP = """\
Check and back up the factory's libSQL (Turso) store through trusted workflows.

  baste store selftest            run the coordination test matrix
  baste store selftest --measure  also measure rows read and written
  baste store export --out f.sqlite
  baste store export --run ID --out f.sqlite

selftest runs the same race, lease, retry and backup tests used locally
against the hosted database, in a throwaway namespace that it deletes. export
copies the whole store into a plain SQLite file. Both run in the repository's
Actions with its secrets; this machine never connects to the store.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "store",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("mode", choices=tuple(store.WORKFLOWS), help="selftest or export")
    parser.add_argument("--measure", action="store_true", help="selftest: also measure usage")
    parser.add_argument("--out", type=Path, help="export: save the SQLite file here")
    parser.add_argument("--no-wait", action="store_true", help="start the run and return")
    parser.add_argument("--run", type=int, help="read the result of an earlier run")
    parser.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    for flag in ("--json", "--verbose"):
        parser.add_argument(
            flag, action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )


def _show(context: Context, data: dict[str, object]) -> None:
    console = context.console
    status = str(data.get("status") or "unknown")
    if data.get("mode") == "selftest":
        console.heading(f"Store self-test: {status}")
        for key in ("passed", "failed", "skipped"):
            console.line(f"  {key}: {data.get(key, 0)}")
    else:
        console.heading(f"Store export: {status}")
        rows = data.get("rows")
        if isinstance(rows, dict):
            for table, count in sorted(rows.items()):
                console.line(f"  {table}: {count}")
    usage = data.get("usage")
    if isinstance(usage, dict):
        console.line(
            f"  Rows read: {usage.get('rows_read', 0)}; rows written: "
            f"{usage.get('rows_written', 0)}"
        )
    _show_bridge(console, data)


def cmd_store(context: Context, arguments: argparse.Namespace) -> Outcome:
    if arguments.measure and arguments.mode != "selftest":
        raise UsageError("--measure works with selftest only")
    if arguments.out and arguments.mode != "export":
        raise UsageError("--out works with export only")
    if arguments.out and arguments.no_wait:
        raise UsageError("--out needs a finished run: leave out --no-wait")
    console = context.console
    try:
        if arguments.run:
            data = store.earlier(context.github, arguments.mode, arguments.run)
            if data.get("result") == "pending":
                _show_bridge(console, data)
                console.line("The run has not finished. Try again shortly.")
                return Outcome(Result.PENDING, EXIT_CODES[Result.PENDING], data)
        elif arguments.no_wait:
            data = store.start(context.github, arguments.mode, measure=arguments.measure)
            console.ok(f"Started the store {arguments.mode} run")
            _show_bridge(console, data)
            console.line(f"Read it with: baste store {arguments.mode} --run ID")
            return Outcome(Result.SUBMITTED, EXIT_OK, data)
        else:
            console.waiting(f"Running the store {arguments.mode} workflow on GitHub")
            data = store.run(
                context.github,
                context.clock,
                arguments.mode,
                measure=arguments.measure,
                started=context.progress,
            )
            console.finish()
        if arguments.out:
            run = data.get("bridge")
            run_id = int(run["run_id"]) if isinstance(run, dict) else 0
            saved = store.download(context.github, run_id, arguments.out)
            data = {**data, "file": str(saved)}
    except bridge.BridgeError as exc:
        return _bridge_failed(context, exc, f"The store {arguments.mode} run failed")
    except (GitHubError, FileExistsError) as exc:
        console.finish()
        console.fail(str(exc))
        return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], {"status": "failed"})
    _show(context, data)
    if "file" in data:
        console.ok(f"Saved {data['file']}")
    passed = data.get("status") == "passed" or data.get("status") == "ok"
    result = Result.COMPLETED if passed else Result.FAILED
    return Outcome(result, EXIT_OK if passed else EXIT_CODES[Result.FAILED], data)
