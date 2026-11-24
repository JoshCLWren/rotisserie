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
Check, back up and fill the factory's libSQL (Turso) store through trusted workflows.

  baste store selftest            run the coordination test matrix
  baste store selftest --measure  also measure rows read and written
  baste store export --out f.sqlite
  baste store export --run ID --out f.sqlite
  baste store migrate --dry-run   count what the Neon copy would move; write nothing
  baste store migrate             copy the Neon state into the store, then count it back

selftest runs the same race, lease, retry and backup tests used locally
against the hosted database, in a throwaway namespace that it deletes. export
copies the whole store into a SQLite file and uploads it encrypted with the
FACTORY_EXPORT_KEY secret; --out decrypts it here with FACTORY_EXPORT_KEY set
to the same key. migrate copies every Neon row family the store keeps and
refuses to report success unless each copied row is found in the store; a dry
run also reports the projection key count, the provider budget rows that hold
a setting and every Neon table's row count. All run in the repository's
Actions with its secrets; this machine never connects to Neon or the store.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "store",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("mode", choices=tuple(store.WORKFLOWS), help="selftest, export or migrate")
    parser.add_argument("--measure", action="store_true", help="selftest: also measure usage")
    parser.add_argument("--dry-run", action="store_true", help="migrate: read and count only")
    parser.add_argument("--out", type=Path, help="export: save the SQLite file here")
    parser.add_argument("--no-wait", action="store_true", help="start the run and return")
    parser.add_argument("--run", type=int, help="read the result of an earlier run")
    parser.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    for flag in ("--json", "--verbose"):
        parser.add_argument(
            flag, action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )


def _show_migrate(context: Context, data: dict[str, object], status: str) -> None:
    console = context.console
    console.heading(f"Store {'dry run' if data.get('dry_run') else 'migration'}: {status}")
    if data.get("error"):
        console.line(f"  error: {data['error']}")
    checks = data.get("checks")
    if isinstance(checks, dict):
        console.line(f"  Projection keys in Neon: {checks.get('projection_keys', 0)}")
        for item in checks.get("provider_budget_set") or []:
            if isinstance(item, dict):
                columns = ", ".join(str(c) for c in item.get("columns") or [])
                console.line(f"  Budget row {item.get('source')}: {columns}")
    tables = data.get("neon_tables")
    if isinstance(tables, dict):
        for table, count in sorted(tables.items()):
            console.line(f"  Neon {table}: {count} rows")
    absent = data.get("absent_tables")
    for table in absent if isinstance(absent, list) else []:
        console.line(f"  Neon {table}: absent")
    groups = data.get("migration")
    if isinstance(groups, dict):
        for name, group in sorted(groups.items()):
            if not isinstance(group, dict):
                continue
            skipped = group.get("skipped") or {}
            reasons = ", ".join(f"{k} {v}" for k, v in sorted(skipped.items()))
            console.line(
                f"  {name}: source {group.get('source', 0)}, eligible {group.get('eligible', 0)},"
                f" inserted {group.get('inserted', 0)}, present {group.get('present', 0)}"
                + (f"; skipped {reasons}" if reasons else "")
            )
    mismatches = data.get("mismatches")
    for item in mismatches if isinstance(mismatches, list) else []:
        console.line(f"  Mismatch: {item}")


def _show(context: Context, data: dict[str, object]) -> None:
    console = context.console
    status = str(data.get("status") or "unknown")
    if data.get("mode") == "migrate":
        _show_migrate(context, data, status)
    elif data.get("mode") == "selftest":
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


def _run_id(data: dict[str, object]) -> str:
    run = data.get("bridge")
    return str(run.get("run_id") or "ID") if isinstance(run, dict) else "ID"


def cmd_store(context: Context, arguments: argparse.Namespace) -> Outcome:
    if arguments.measure and arguments.mode != "selftest":
        raise UsageError("--measure works with selftest only")
    if arguments.out and arguments.mode != "export":
        raise UsageError("--out works with export only")
    if arguments.dry_run and arguments.mode != "migrate":
        raise UsageError("--dry-run works with migrate only")
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
            data = store.start(
                context.github,
                arguments.mode,
                measure=arguments.measure,
                dry_run=arguments.dry_run,
            )
            console.ok(f"Started the store {arguments.mode} run")
            _show_bridge(console, data)
            console.line(f"Read it with: baste store {arguments.mode} --run {_run_id(data)}")
            return Outcome(Result.SUBMITTED, EXIT_OK, data)
        else:
            console.waiting(f"Running the store {arguments.mode} workflow on GitHub")
            data = store.run(
                context.github,
                context.clock,
                arguments.mode,
                measure=arguments.measure,
                dry_run=arguments.dry_run,
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
