from __future__ import annotations

import argparse

from rotisserie.factory_console.commands import (
    _shared,
    backoffs,
    crashes,
    db_usage,
    heal,
    incident,
    labels,
    recap,
    schema,
    self_review,
    store,
    turning,
)
from rotisserie.factory_console.commands._shared import ACTIONS
from rotisserie.factory_console.commands.parser_insight import (
    _common,
    _via,
    add_insight_parsers,
)
from rotisserie.factory_console.commands.parser_ops import add_operator_parsers
from rotisserie.factory_console.service import external


def add_parser(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    prog: str | None = None,
    epilog: str | None = None,
) -> None:
    factory = subcommands.add_parser(
        "factory",
        help="operate the software factory: start work, watch it, see what happened",
        description=_shared.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        prog=prog,
        epilog=epilog,
    )
    factory.add_argument("--repo", help="repository running the factory (owner/name)")
    factory.add_argument(
        "--json", action="store_true", help="print one JSON document, no progress text"
    )
    factory.add_argument("--verbose", action="store_true", help="also show raw diagnostic detail")
    factory.add_argument(
        "--progress", action="store_true", help="write one JSON line per step to stderr"
    )
    factory.add_argument(
        "--max-age", type=int, default=0, help="reads: reuse cached GitHub answers"
    )
    commands = factory.add_subparsers(dest="factory_command", metavar="COMMAND")

    run = commands.add_parser(
        "run",
        help="start a worker on the next task or a chosen issue/PR",
        description=(
            "Start a factory worker. With no arguments, menus ask for the provider, "
            "model and work. With PROVIDER and MODEL given, it starts immediately."
        ),
    )
    run.add_argument("provider", nargs="?", help="factory provider, e.g. opencode-go")
    run.add_argument("model", nargs="?", help="exact model ID (provider/model) or auto")
    run.add_argument(
        "--force",
        "--force-slot",
        dest="force",
        action="store_true",
        help="override every factory limit; see 'Override' below",
    )
    run.add_argument(
        "--slot", type=int, help="use worker number N for this provider (implies --force)"
    )
    run.add_argument("--target", help="work on issue:N or pr:N instead of the next task")
    run.add_argument(
        "--exact",
        action="store_true",
        help="fail if MODEL is unavailable instead of using another model from the provider",
    )
    run.add_argument(
        "--action",
        choices=ACTIONS,
        default="advance",
        help="what to do on a chosen PR (with --force and --target)",
    )
    run.add_argument(
        "--take-over",
        action="store_true",
        help="if another worker holds the target, stop it and take over (implies --force)",
    )
    wait = run.add_mutually_exclusive_group()
    wait.add_argument(
        "--no-wait",
        "--detach",
        dest="no_wait",
        action="store_true",
        help="submit and return immediately",
    )
    wait.add_argument(
        "--follow",
        action="store_true",
        help="keep watching until the worker finishes and the PR is checked",
    )
    run.add_argument("--yes", "-y", action="store_true", help="skip the confirmation question")
    _common(run)

    watch = commands.add_parser("watch", help="resume watching a request (run ID or URL)")
    watch.add_argument("run", help="scheduler or worker run ID or URL")
    watch.add_argument("--follow", action="store_true", help="watch until the worker finishes")
    watch.add_argument("--decision", action="store_true", help="stop at the scheduler's decision")
    _common(watch)

    status = commands.add_parser("status", aliases=["who"], help="who is working on what right now")
    _common(status)

    cancel = commands.add_parser("cancel", help="stop the worker on an issue/PR and release it")
    cancel.add_argument("what", help="issue:N, pr:N, or a worker run ID")
    cancel.add_argument(
        "--no-wait", action="store_true", help="do not wait for the claim to be released"
    )
    _common(cancel)

    report = commands.add_parser("report", help="what happened since you last looked")
    report.add_argument(
        "--since", default="12h", help="how far back: 12h, 2d, or a date (default 12h)"
    )
    _common(report)

    stats = commands.add_parser("stats", help="which providers and models actually deliver")
    stats.add_argument("--window", choices=("24h", "7d"), default="7d")
    stats.add_argument("--provider", help="only this provider")
    _common(stats)

    settings = commands.add_parser("settings", help="view or change factory limits and priorities")
    settings.add_argument(
        "change",
        nargs="*",
        metavar="NAME [VALUE]",
        help="NAME VALUE to set, or NAME --reset to restore the default",
    )
    settings.add_argument("--reset", action="store_true", help="restore the default for NAME")
    _common(settings)

    prioritize = commands.add_parser("prioritize", help="move an issue or PR up the queue")
    prioritize.add_argument("what", help="issue:N or pr:N")
    prioritize.add_argument(
        "--level", choices=("critical", "high", "medium", "low", "none"), default="critical"
    )
    _common(prioritize)

    explain = commands.add_parser(
        "why",
        aliases=["explain"],
        help="why the factory is or is not picking up an issue",
        description=(
            "Explain, from the dispatcher's own rules, why an issue would or would not be "
            "picked up right now. With no argument, summarize every open issue."
        ),
    )
    explain.add_argument("what", nargs="?", help="issue:N (or just N); omit for every open issue")
    _via(explain)
    _common(explain)

    next_work = commands.add_parser(
        "next",
        help="read the ranked work frontier for external agents (never claims work)",
        description=(
            "Inspect versioned, ranked controller candidates and all known holds. "
            "Store ownership is authoritative and unavailable lease evidence holds closed. "
            "The result is read-only; a separate trusted claim is required before work."
        ),
    )
    _via(next_work)
    _common(next_work)

    wait_work = commands.add_parser(
        "wait-for-work",
        help="wait until there is work an outside agent can reserve",
        description=(
            "Check the ranked work list until it offers an unheld candidate for an action "
            "you can do, then show it. Nothing is reserved: run reserve-next next."
        ),
    )
    wait_work.add_argument(
        "--can",
        action="append",
        choices=sorted(external.ACTIONS),
        default=[],
        help="action the agent can perform (repeat; default all supported actions)",
    )
    wait_work.add_argument(
        "--interval",
        type=int,
        default=None,
        help="seconds between checks (default 60, or 300 with --via github)",
    )
    wait_work.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="give up after this many seconds (default 3600; 0 waits until stopped)",
    )
    wait_work.add_argument(
        "--claim",
        action="store_true",
        help="claim the first takeable item by label and show the controller's answer",
    )
    _via(wait_work)
    _common(wait_work)

    claim_work = commands.add_parser(
        "claim",
        help="request an item by adding factory:claimed and show the controller's answer",
    )
    claim_work.add_argument("what", help="issue:N or pr:N")
    claim_work.add_argument("--no-wait", action="store_true", help="add the label and return")
    claim_work.add_argument("--timeout", type=int, default=600, help="seconds to wait (600)")
    _common(claim_work)

    finish_work = commands.add_parser(
        "finish",
        help="end your label claim (remove factory:claimed) and show the hand-off result",
    )
    finish_work.add_argument("what", help="issue:N or pr:N")
    finish_work.add_argument("--no-wait", action="store_true", help="remove the label and return")
    finish_work.add_argument("--timeout", type=int, default=600, help="seconds to wait (600)")
    _common(finish_work)

    add_insight_parsers(commands)

    track = commands.add_parser(
        "follow",
        help="show each lifecycle change of an item until it finishes or is held",
    )
    track.add_argument("what", help="issue:N or pr:N")
    track.add_argument("--once", action="store_true", help="show the current state and exit")
    track.add_argument("--interval", type=int, default=60, help="seconds between checks (60)")
    track.add_argument("--timeout", type=int, default=3600, help="give up after (3600; 0 never)")
    _common(track)

    reserve = commands.add_parser(
        "reserve-next",
        help="reserve the highest ranked eligible work for an external agent",
    )
    reserve.add_argument("--client", required=True, help="stable agent/client identifier")
    reserve.add_argument("--target", default="", help="optional expected top offer (issue:N/pr:N)")
    reserve.add_argument(
        "--advertised-at", default="", help="optional timestamp from a frontier manifest"
    )
    reserve.add_argument(
        "--can",
        action="append",
        choices=sorted(external.ACTIONS),
        default=[],
        help="action the agent can perform (repeat; default all supported actions)",
    )
    _via(reserve)
    _common(reserve)

    revoke = commands.add_parser(
        "revoke", help="remove an external agent's claim (operator override, logged)"
    )
    revoke.add_argument("claim_id", help="claim UUID shown by 'leases' or 'next'")
    revoke.add_argument("--reason", required=True, help="why the claim is being removed")
    _common(revoke)

    for verb in ("renew", "release", "handoff"):
        lease = commands.add_parser(verb, help=f"{verb} an external fenced claim")
        lease.add_argument("claim_id", help="UUID returned by reserve-next")
        lease.add_argument("--client", required=True, help="the original agent/client identifier")
        if verb == "handoff":
            lease.add_argument(
                "--evidence", required=True, help="same-repository GitHub issue, PR, or run URL"
            )
        _via(lease)
        _common(lease)

    clear = commands.add_parser(
        "unstick",
        help="start the workers that clear whatever is holding the factory back",
        description=(
            "Read why work is not starting, plan the work that clears it (for a full review "
            "backlog: review and repair those pull requests), pick the provider and model with "
            "the best delivered track record, and start one worker per task after you confirm."
        ),
    )
    clear.add_argument("provider", nargs="?", help="use this provider instead of the best one")
    clear.add_argument("model", nargs="?", help="use this exact model (with PROVIDER)")
    clear.add_argument("--max", type=int, dest="max_workers", help="start at most N workers")
    clear.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="ITEM",
        help="never start this item (pr:N or issue:N); repeatable",
    )
    clear.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ITEM",
        help="start only these items (pr:N or issue:N); repeatable",
    )
    clear.add_argument("--dry-run", action="store_true", help="show the plan; start nothing")
    clear.add_argument("--yes", "-y", action="store_true", help="start without asking")
    _common(clear)

    results = commands.add_parser(
        "outcomes",
        help="did requested work actually happen? (worker result and the target's state now)",
        description=(
            "Follow each recent scheduler request through to its effect: what the scheduler "
            "decided, what the worker reported, and whether the goal holds now (merge conflict "
            "gone, checks fixed, review decided, PR delivered)."
        ),
    )
    results.add_argument("--since", default="2h", help="how far back: 2h, 1d, or a date")
    results.add_argument(
        "--all", action="store_true", help="include routine scheduler passes, not just targeted"
    )
    results.add_argument(
        "--by-item",
        action="store_true",
        help="one line per item: rounds, misses, latest result (find items going in circles)",
    )
    results.add_argument(
        "--limit", type=int, default=40, help="scheduler passes to read (default 40, newest first)"
    )
    _common(results)

    shut = commands.add_parser(
        "close",
        help="close a pull request that should not be done, and the issue it delivers",
        description=(
            "Stop any worker holding the pull request, close it with your reason as the "
            "comment, and close the issue it delivers as 'not planned' so intake does not "
            "pick it up again."
        ),
    )
    shut.add_argument("what", nargs="+", help="pr:N (one or more)")
    shut.add_argument("--reason", required=True, help="why; posted as the closing comment")
    shut.add_argument(
        "--keep-issue", action="store_true", help="close only the pull request, not its issue"
    )
    shut.add_argument("--yes", "-y", action="store_true", help="do not ask first")
    _common(shut)

    health = commands.add_parser(
        "doctor", help="is the factory itself healthy (stats, dashboard, scheduler, gates)"
    )
    health.add_argument(
        "--fix",
        action="store_true",
        help="make the safe repairs it finds (e.g. re-run the stats publisher when stale)",
    )
    _common(health)

    quota = commands.add_parser(
        "limits", help="show GitHub API usage, reset times and retry information"
    )
    _common(quota)

    event = commands.add_parser(
        "label-event", help="handle one GitHub label-claim event (trusted Actions only)"
    )
    event.add_argument("--event-path", required=True, help="the GITHUB_EVENT_PATH payload")
    _common(event)

    syncer = commands.add_parser(
        "sync",
        help="merge main into factory PRs, only while no worker holds them",
        description=(
            "Merge the default branch into each pull request with GitHub's update-branch, "
            "refusing when its head moved (so a worker's push is never overwritten) and while "
            "a worker holds it. Store leases decide ownership where the store is configured; "
            "otherwise owner labels do, and the output says the store was not consulted. A PR that "
            "conflicts with main is reported and left to merge-conflict work."
        ),
    )
    syncer.add_argument("what", nargs="*", help="pr:N (one or more)")
    syncer.add_argument(
        "--all-stale",
        action="store_true",
        help="every factory PR that is red only because main has since moved past the failure",
    )
    syncer.add_argument(
        "--contains", default="", metavar="SHA", help="done once the head contains this commit"
    )
    syncer.add_argument("--max", type=int, default=5, help="sync at most N PRs (default 5)")
    syncer.add_argument(
        "--now",
        action="store_true",
        help=(
            "without the store, sync right away on owner labels instead of queueing for the "
            "scheduler's leased sync (a worker assigned mid-sync is possible)"
        ),
    )
    syncer.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    syncer.add_argument("--yes", "-y", action="store_true", help="sync without asking")
    _common(syncer)

    holds = commands.add_parser(
        "leases",
        help="who holds which factory item (store leases, or owner labels without the store)",
    )
    holds.add_argument("what", nargs="*", help="pr:N or issue:N (default: every lease)")
    holds.add_argument(
        "--release-expired",
        action="store_true",
        help="delete lease rows that expired over an hour ago (never an active claim)",
    )
    _via(holds)
    _common(holds)

    groom = commands.add_parser(
        "groom",
        help="inventory the whole open backlog, then apply reviewed corrections",
        description=(
            "inventory reads every open issue and PR with evidence for each; apply checks a "
            "reviewed JSON plan against live state and, with --yes, performs it."
        ),
    )
    groom_steps = groom.add_subparsers(dest="step", required=True)
    survey = groom_steps.add_parser("inventory", help="every open issue and PR, with evidence")
    survey.add_argument("--out", help="also write the full inventory (JSON) to this file")
    survey.add_argument(
        "--all", action="store_true", help="list every item, not only those with evidence"
    )
    survey.add_argument(
        "--no-timelines",
        action="store_true",
        help="skip per-issue merged-PR lookups (faster, less evidence)",
    )
    _common(survey)
    fix = groom_steps.add_parser("apply", help="check a reviewed plan; --yes performs it")
    fix.add_argument("plan", help="JSON plan file (see docs/factory-console.md)")
    fix.add_argument("--yes", "-y", action="store_true", help="perform it (default: dry run)")
    fix.add_argument("--log", help="append each result (JSON lines) to this audit file")
    _common(fix)

    gate = commands.add_parser(
        "review",
        help="whose review counts on a pull request: known self-review versus missing metadata",
    )
    gate.add_argument("what", nargs="*", help="pr:N (one or more)")
    gate.add_argument(
        "--blocked", action="store_true",
        help="every open factory PR parked in factory:blocked",
    )  # fmt: skip
    gate.add_argument(
        "--requeue", action="store_true",
        help="move PRs the retired review gate blocked back to review for another model",
    )  # fmt: skip
    gate.add_argument("--yes", action="store_true", help="requeue without asking")
    _common(gate)

    for name, words, text in (
        ("providers", "[disable|enable NAME]", _shared.PROVIDERS_HELP),
        ("models", "PROVIDER | disable|enable PROVIDER MODEL", _shared.MODELS_HELP),
    ):
        fleet = commands.add_parser(
            name,
            help=text.splitlines()[0],
            description=text,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        fleet.add_argument("change" if name == "providers" else "words", nargs="*", metavar=words)
        fleet.add_argument("--reason", default="", help="why it is turned off (shown in listings)")
        if name == "models":
            fleet.add_argument("--billing", **_shared.BILLING_OPTION)
        _common(fleet)
    incident.add_parser(commands)
    backoffs.add_parser(commands)
    db_usage.add_parser(commands)
    labels.add_parser(commands)
    crashes.add_parser(commands)
    recap.add_parser(commands)
    self_review.add_parser(commands)
    heal.add_parser(commands)
    store.add_parser(commands)
    schema.add_parser(commands)
    turning.add_parser(commands)
    add_operator_parsers(commands)
