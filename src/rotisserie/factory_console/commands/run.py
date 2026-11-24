from __future__ import annotations

import argparse
import os

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import (
    ACTION_WORDS,
    FORCE_EXPLANATION,
    TAKE_OVER_ATTEMPTS,
    TARGET,
    Outcome,
    Request,
    _split_target,
    discover_models,
)
from rotisserie.factory_console.commands.status import _stop_worker
from rotisserie.factory_console.commands.watch import _watch_scheduler
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import dispatch, external, fleet
from rotisserie.factory_console.tracker import EXIT_CODES, Result, Tracker
from rotisserie.factory_console.ui import Console, local_time, since
from rotisserie.factory_console.work import Item, current_work, lookup, open_items


def cmd_run(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    explicit = bool(arguments.provider and arguments.model)
    if arguments.slot is not None and arguments.slot < 1:
        raise UsageError("--slot must be 1 or more.")
    if arguments.target and not TARGET.match(arguments.target):
        raise UsageError("--target must look like issue:123 or pr:123.")
    if arguments.action != "advance" and not arguments.target:
        raise UsageError("--action needs --target: choose the pull request to work on.")
    force = bool(
        arguments.force
        or arguments.slot is not None
        or arguments.take_over
        or arguments.action != "advance"
    )
    if arguments.provider:
        provider = runtime.resolve_provider(arguments.provider)
    elif context.interactive:
        provider = _pick_provider(context)
    else:
        raise UsageError("Name a provider (see 'baste providers'), or run in a terminal for menus.")
    if arguments.model:
        model = arguments.model
    elif context.interactive:
        model = _pick_model(context, provider)
    else:
        model = "auto"
    runtime.validate_model(provider, model)
    request = Request(
        provider,
        model,
        arguments.target or "",
        force,
        arguments.slot,
        arguments.action,
        arguments.take_over,
    )
    # An explicit provider and model is a complete request: no further questions.
    if context.interactive and not explicit:
        if not request.target:
            chosen = _pick_work(context, request)
            if chosen is None:
                return Outcome(Result.NO_WORK, 0, {"cancelled": True})
        if not request.force:
            request.force = (
                context.prompt.choose(
                    "Respect the factory's limits for this run?",
                    [
                        "Yes, use normal scheduling",
                        "No, force it (ignore open-PR limits, cooldowns, worker counts, pauses)",
                    ],
                    default=1,
                )
                == 1
            )
    _show_request(console, request)
    if context.interactive and not explicit and not arguments.yes:
        if not context.prompt.confirm("Start this?"):
            console.line("Nothing was started.")
            return Outcome(Result.NO_WORK, 0, {"cancelled": True})
    data = context.progress
    data["request"] = request.to_dict()
    if request.target:
        data["target_ref"] = request.target
    if not (request.target and request.take_over):
        return _submit_and_watch(
            context, request, data, no_wait=arguments.no_wait, follow=arguments.follow
        )
    # Between releasing the old worker and this request reaching the scheduler, a
    # routine pass can hand the item to another worker. A takeover therefore waits
    # for the scheduler's decision (even with --no-wait) and, if the item was
    # re-claimed in that gap, takes it over once more.
    kind, number = _split_target(request.target)
    outcome = Outcome(Result.UNKNOWN, EXIT_CODES[Result.UNKNOWN], data)
    stopped_workers: list[str] = []
    attempt = 0
    # An outside claim may hold the PR's linked issue rather than the PR
    # itself (an implementation claim that has just opened its PR).
    for held in external.claim_scope(context.github, kind, number):
        if not _take_over_external(context, held, data):
            return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], data)
    for attempt in range(TAKE_OVER_ATTEMPTS):
        item = lookup(context.github, kind, number)
        if item.owner:
            stopped = _stop_worker(context, item, wait=True, purpose="take over")
            stopped_workers.append(item.owner)
            data["stopped_workers"] = list(stopped_workers)
            data["stopped_worker"] = item.owner
            if not stopped:
                return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], data)
        outcome = _submit_and_watch(
            context,
            request,
            data,
            no_wait=False,
            follow=arguments.follow,
            decision_only=arguments.no_wait,
        )
        if outcome.result is not Result.BUSY or attempt + 1 == TAKE_OVER_ATTEMPTS:
            break
        console.warn(
            f"Another worker claimed #{number} before this request landed; taking it over again"
        )
    data["take_over_attempts"] = attempt + 1
    return outcome


def _take_over_external(context: Context, number: int, data: dict[str, object]) -> bool:
    """Remove an external agent's claim on the target; the operator asked to take over.

    External agents hold no GitHub worker run to cancel, so without this a
    takeover would keep finding the target busy until it gave up.
    """
    console = context.console
    try:
        claim = external.holder(context.github, number)
    except external.ClaimStoreUnavailable as exc:
        console.note(
            f"Can't see outside-agent claims from here ({exc}). If one holds #{number}, "
            "run 'baste revoke' where the factory's Neon connection is set up."
        )
        return True
    except (RuntimeError, ValueError) as exc:
        console.fail(f"Could not check whether an outside agent holds #{number}: {exc}")
        return False
    if claim is None:
        return True
    who = f"{claim.get('client')} ({claim.get('actor')})"
    console.note(f"#{number} is held by outside agent {who}; removing its claim")
    try:
        result = external.revoke(
            context.github,
            claim_id=str(claim.get("claim_id")),
            reason=f"operator take-over of #{number}",
            environment=dict(os.environ),
        )
    except (RuntimeError, ValueError) as exc:
        console.fail(f"Could not remove the outside agent's claim: {exc}")
        return False
    if result.get("status") != "revoked":
        console.fail(f"Could not remove the outside agent's claim: {result.get('reason')}")
        return False
    data["revoked_external_claim"] = result
    console.ok("Outside agent's claim removed", who)
    return True


def _show_request(console: Console, request: Request) -> None:
    console.heading("Factory")
    rows = [
        ("Provider", request.provider.name),
        ("Model", "chosen automatically" if request.model == "auto" else request.model),
        ("Override", "On — " + FORCE_EXPLANATION if request.force else "Off — normal scheduling"),
        ("Work", _describe_target(request)),
    ]
    if request.slot is not None:
        rows.append(("Worker", f"number {request.slot}"))
    console.fields(rows)
    console.line()


def _describe_target(request: Request) -> str:
    if not request.target:
        return "next task in the queue"
    kind, number = _split_target(request.target)
    text = f"{'issue' if kind == 'issue' else 'pull request'} #{number}"
    if request.action != "advance":
        text += f" ({ACTION_WORDS[request.action]})"
    if request.take_over:
        text += ", taking over from any current worker"
    return text


def _submit_and_watch(
    context: Context,
    request: Request,
    data: dict[str, object],
    *,
    no_wait: bool,
    follow: bool,
    decision_only: bool = False,
) -> Outcome:
    console = context.console
    submission = dispatch.submit(context.github, context.clock, request)
    console.debug(submission.raw.strip())
    console.ok("Request sent", local_time(submission.submitted_at))
    run_id = submission.run_id
    if run_id is None:
        console.fail("GitHub accepted the request but the scheduler run could not be found.")
        console.link("Look here", submission.url)
        return Outcome(Result.UNKNOWN, EXIT_CODES[Result.UNKNOWN], data)
    data["scheduler_run_id"] = run_id
    data["scheduler_url"] = submission.url
    if no_wait:
        console.link("Scheduler", submission.url)
        console.note(f"Follow it later: baste watch {run_id}")
        return Outcome(Result.SUBMITTED, EXIT_CODES[Result.SUBMITTED], data)
    tracker = Tracker(context.github, context.clock)
    return _watch_scheduler(
        context,
        tracker,
        run_id,
        submission.submitted_at,
        data,
        follow=follow,
        request=request,
        decision_only=decision_only,
    )


def _pick_provider(context: Context) -> runtime.Provider:
    providers = runtime.providers()
    switched = fleet.read(context.github)
    labels = [
        f"{p.name}  ({p.key})" + ("  [turned off]" if switched.provider_off(p.source) else "")
        for p in providers
    ]
    return providers[context.prompt.choose("Which provider?", labels)]


def _pick_model(context: Context, provider: runtime.Provider) -> str:
    if not provider.model_prefix:
        return "auto"
    context.console.note("Looking up models…")
    models = discover_models(provider)[:40]
    options = (
        ["Let the factory choose a working model"]
        + [model for model, _ in models]
        + ["Type an exact model ID"]
    )
    index = context.prompt.choose("Which model?", options, default=0)
    if index == 0:
        return "auto"
    if index == len(options) - 1:
        return context.prompt.ask(f"Exact model ID (starts with {provider.model_prefix}):")
    return options[index]


def _pick_work(context: Context, request: Request) -> str | None:
    choice = context.prompt.choose(
        "What should it work on?",
        [
            "The next task in the queue",
            "Choose an open issue",
            "Choose an open pull request",
            "Type an issue or PR number",
        ],
        default=0,
    )
    if choice == 0:
        return ""
    if choice == 3:
        return _ask_target(context, request)
    kind = "issue" if choice == 1 else "pr"
    now = context.clock.now()
    entries = [item for item in open_items(context.github, limit=200) if item.kind == kind][:40]
    runs = {item.owner for item in entries if item.owner}
    if runs:
        work = {item.number: item for item in current_work(context.github, with_session=False)}
        for item in entries:
            if item.number in work:
                item.run = work[item.number].run
    options = []
    for item in entries:
        line = f"#{item.number}  {item.title[:70]}"
        if item.owner:
            running = since(item.run.started, now) if item.run else since(item.updated, now)
            line += f"   [{item.provider}, {running}]"
        options.append(line)
    if not options:
        context.console.line(f"No open {'issues' if kind == 'issue' else 'pull requests'}.")
        return _ask_target(context, request)
    index = context.prompt.choose(
        f"Open {'issues' if kind == 'issue' else 'pull requests'} "
        "([provider, time] = someone is working on it)",
        options + ["Type a number instead"],
    )
    if index == len(options):
        return _ask_target(context, request)
    item = entries[index]
    request.target = item.ref
    if item.owner:
        return _resolve_busy(context, request, item)
    return request.target


def _ask_target(context: Context, request: Request) -> str | None:
    while True:
        reply = context.prompt.ask("Issue or PR (e.g. issue:123, pr:45):")
        if TARGET.match(reply):
            request.target = reply
            item = lookup(context.github, *_split_target(reply))
            return _resolve_busy(context, request, item) if item.owner else reply
        context.console.line("Use issue:NUMBER or pr:NUMBER.")


def _resolve_busy(context: Context, request: Request, item: Item) -> str | None:
    running = since(item.run.started, context.clock.now()) if item.run else "unknown time"
    choice = context.prompt.choose(
        f"#{item.number} is being worked on by {item.provider} for {running}. What now?",
        [
            f"Take it over with {request.provider.name} ({request.model})",
            "Stop that work and leave it unclaimed",
            "Leave it and pick something else",
        ],
    )
    if choice == 0:
        request.take_over = True
        request.force = True
        return request.target
    if choice == 1:
        _stop_worker(context, item, wait=True, purpose="stop")
        return None
    request.target = ""
    return _pick_work(context, request)
