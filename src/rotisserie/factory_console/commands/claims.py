from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome, _rows
from rotisserie.factory_console.commands.why import _bridge_failed, _show_bridge
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import bridge, claimops, labelclaims, labels
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.ui import Console


def _show_claim_answer(console: Console, data: dict[str, object]) -> Outcome:
    status = str(data.get("status") or "unknown")
    comment = str(data.get("status_comment") or "")
    run = data.get("run")
    if status in ("reserved", "released", "already-held"):
        console.ok(f"{data.get('target')}: {status}")
    elif status in ("requested",):
        console.ok(f"{data.get('target')}: label changed; the controller answers on the item")
    else:
        console.warn(f"{data.get('target')}: {status} ({data.get('reason', 'no reason given')})")
    for line in comment.splitlines():
        if line.strip() and not line.startswith("**"):
            console.line(f"  {line.strip()}")
    if isinstance(run, dict):
        console.line(f"  Controller run: {run.get('url')}")
    if data.get("follow"):
        console.line(f"  Follow: {data['follow']}")
    good = status in ("reserved", "released", "already-held", "requested")
    result = (
        Result.COMPLETED if good else Result.NO_WORK if status == "conflict" else Result.UNKNOWN
    )
    return Outcome(result, EXIT_OK if good else EXIT_CODES[result], data)


def cmd_claim(context: Context, arguments: argparse.Namespace) -> Outcome:
    claimops.parse_target(arguments.what)
    context.console.waiting(f"Requesting {arguments.what} and waiting for the controller")
    data = claimops.claim(
        context.github,
        context.clock,
        arguments.what,
        wait=not arguments.no_wait,
        timeout=float(arguments.timeout),
    )
    context.console.finish()
    return _show_claim_answer(context.console, data)


def cmd_finish(context: Context, arguments: argparse.Namespace) -> Outcome:
    claimops.parse_target(arguments.what)
    context.console.waiting(f"Finishing {arguments.what} and waiting for the hand-off")
    data = claimops.finish(
        context.github,
        context.clock,
        arguments.what,
        wait=not arguments.no_wait,
        timeout=float(arguments.timeout),
    )
    context.console.finish()
    return _show_claim_answer(context.console, data)


def cmd_runs(context: Context, arguments: argparse.Namespace) -> Outcome:
    _, number = claimops.parse_target(arguments.what)
    if not 1 <= arguments.limit <= 20:
        raise UsageError("--limit must be between 1 and 20")
    rows = claimops.item_runs(context.github, number, limit=arguments.limit)
    console = context.console
    console.heading(f"Label claim controller runs for {arguments.what}")
    for row in rows:
        row["result"] = claimops.run_result(context.github, row)
        outcome = row["result"] if isinstance(row["result"], dict) else {}
        verdict = outcome.get("status") or outcome.get("result") or row.get("conclusion")
        reason = outcome.get("reason") or outcome.get("error") or ""
        failed = outcome.get("failed_request")
        detail = f"{reason} ({failed})" if failed else str(reason)
        console.line(
            f"  {row.get('created_at')} {row.get('event')} by {row.get('actor')}: "
            f"{verdict}{' - ' + detail if detail else ''}"
        )
        console.line(f"    {row.get('url')}")
    if not rows:
        console.line("  No controller run for this item in the last 50 runs.")
    comment = claimops.status_comment(context.github, number)
    data: dict[str, object] = {"target": arguments.what, "runs": rows, "status_comment": comment}
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _describe_view(view: dict[str, object]) -> str:
    parts = [str(view.get("state"))]
    for key in ("stage", "owner", "checks"):
        if view.get(key):
            parts.append(f"{key} {view[key]}")
    if view.get("merged"):
        parts.append("merged")
    claims = view.get("claim_labels")
    if claims:
        parts.append(", ".join(map(str, _rows_or_list(claims))))
    issue = view.get("issue")
    if isinstance(issue, dict):
        parts.append(
            f"{issue.get('target')} {issue.get('state')} {issue.get('stage') or ''}".strip()
        )
    return "; ".join(parts)


def cmd_follow(context: Context, arguments: argparse.Namespace) -> Outcome:
    claimops.parse_target(arguments.what)
    console = context.console
    if arguments.once:
        view = claimops.snapshot(context.github, arguments.what)
        console.line(f"{arguments.what}: {_describe_view(view)}")
        return Outcome(Result.COMPLETED, EXIT_OK, view)
    if arguments.interval < 15 or arguments.timeout < 0:
        raise UsageError("--interval must be at least 15 seconds and --timeout 0 or more")

    def changed(view: dict[str, object], keys: list[str]) -> None:
        stamp = context.clock.now().astimezone().strftime("%I:%M:%S %p")
        console.line(f"{stamp} {arguments.what}: {_describe_view(view)}")

    data = claimops.follow(
        context.github,
        context.clock,
        arguments.what,
        interval=float(arguments.interval),
        timeout=float(arguments.timeout),
        on_change=changed,
    )
    if data["status"] == "finished":
        console.ok(f"Done following {arguments.what}: {data.get('reason')}")
        return Outcome(Result.COMPLETED, EXIT_OK, data)
    console.warn(f"Stopped following {arguments.what} after {arguments.timeout}s")
    return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)


def cmd_labels(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    if arguments.delete_legacy and not arguments.repair:
        console.fail("--delete-legacy needs --repair")
        return Outcome(Result.FAILED, 2, {})
    mode = (
        "delete-legacy" if arguments.delete_legacy else "repair" if arguments.repair else "dry-run"
    )
    if arguments.run:
        data = claimops.run_report(context.github, arguments.run)
        _show_label_report(console, data)
        return Outcome(Result.COMPLETED, EXIT_OK, data)
    if arguments.via == "github":
        if not arguments.no_wait:
            console.waiting("Running the label doctor workflow on GitHub")
        try:
            data = claimops.doctor(
                context.github,
                context.clock,
                mode,
                wait=not arguments.no_wait,
                started=context.progress,
            )
        except bridge.BridgeError as exc:
            return _bridge_failed(context, exc, "The label doctor did not report")
        console.finish()
        if arguments.no_wait:
            _show_bridge(console, data)
            return Outcome(Result.SUBMITTED, EXIT_OK, data)
    else:
        if arguments.no_wait:
            raise UsageError("--no-wait needs --via github")
        try:
            if arguments.repair:
                report = labels.repair(
                    context.github, delete_legacy=arguments.delete_legacy, now=context.clock.now()
                )
            else:
                report, _ = labels.audit(context.github, now=context.clock.now())
        except (RuntimeError, ValueError) as exc:
            console.fail(f"Label doctor could not run: {exc}")
            return Outcome(Result.FAILED, 4, {"reason": str(exc)})
        data = report.to_dict()
    _show_label_report(console, data)
    if not data.get("changes") and not data.get("applied"):
        console.ok("No label changes needed.")
    elif mode == "dry-run":
        suffix = " --via github" if arguments.via == "github" else ""
        console.line(f"Apply these changes: baste labels --repair{suffix}")
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _show_label_report(console: Console, data: dict[str, object]) -> None:
    console.heading(f"Label doctor ({data.get('mode', '?')})")
    console.line(f"Authority: {data.get('authority', '?')}")
    shown = _rows(data.get("applied")) or _rows(data.get("changes"))
    for change in shown:
        reasons = "; ".join(str(reason) for reason in _rows_or_list(change.get("reasons")))
        console.line(
            f"  {change.get('target')}: +{change.get('add')} -{change.get('remove')} ({reasons})"
        )
    for hold in _rows(data.get("holds")):
        held = "; ".join(map(str, _rows_or_list(hold.get("holds"))))
        console.warn(f"  {hold.get('target')}: held ({held})")
    legacy = _rows_or_list(data.get("deleted_definitions")) or _rows_or_list(
        data.get("legacy_definitions")
    )
    if legacy:
        verb = "Deleted" if data.get("deleted_definitions") else "Obsolete definitions"
        console.line(f"{verb}: {', '.join(map(str, legacy))}")
    for skip in _rows(data.get("skipped")):
        console.warn(f"  skipped {skip.get('target')}: {skip.get('reason')}")
    _show_bridge(console, data)


def _rows_or_list(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def cmd_label_event(context: Context, arguments: argparse.Namespace) -> Outcome:
    environment = dict(os.environ)
    if environment.get("GITHUB_ACTIONS") != "true":
        context.console.fail("label-event runs only in trusted Actions")
        return Outcome(Result.FAILED, 4, {"status": "refused"})
    payload = json.loads(Path(arguments.event_path).read_text())
    result = labelclaims.handle(
        context.github, environment.get("GITHUB_EVENT_NAME", ""), payload, environment
    )
    if result.get("status") != "ignored":
        try:
            _, number = labelclaims._target(payload)
            result["doctor"] = labelclaims.doctor_after(context.github, number)
        except Exception as exc:  # noqa: BLE001 - the periodic doctor repairs later
            result["doctor"] = {"status": "skipped", "reason": type(exc).__name__}
    context.console.line(json.dumps(result, default=str))
    return Outcome(Result.COMPLETED, EXIT_OK, result)
