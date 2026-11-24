from __future__ import annotations

import argparse
import os

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.claims import _show_claim_answer
from rotisserie.factory_console.commands.why import (
    _bridge_failed,
    _show_bridge,
    _show_offers,
    _snapshot,
)
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import bridge, claimops, external
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.ui import duration


def cmd_wait_for_work(context: Context, arguments: argparse.Namespace) -> Outcome:
    via = arguments.via
    abilities = frozenset(arguments.can) if arguments.can else external.ACTIONS
    interval = arguments.interval
    if interval is None:
        interval = 300 if via == "github" else 60
    # Each GitHub check is a full workflow run; do not flood Actions.
    floor = 120 if via == "github" else 15
    if interval < floor:
        suffix = " with --via github" if via == "github" else ""
        raise UsageError(f"--interval must be at least {floor} seconds{suffix}.")
    if arguments.timeout < 0:
        raise UsageError("--timeout must be 0 or more.")
    console = context.console
    started = context.clock.now()
    checks = 0
    data: dict[str, object] = {}
    login: str | None = None
    while True:
        waited = (context.clock.now() - started).total_seconds()
        if arguments.timeout and waited >= arguments.timeout and checks:
            # No check starts at or after the deadline: a GitHub check is a
            # workflow run that could keep the caller waiting long past it.
            console.finish()
            console.warn(f"No work an outside agent can take after {duration(waited)}.")
            return Outcome(
                Result.NO_WORK,
                EXIT_CODES[Result.NO_WORK],
                {"status": "none", "checks": checks, "frontier": data},
            )
        checks += 1
        limit = bridge.FRONTIER_TIMEOUT
        if arguments.timeout:
            # A check never keeps the caller past the deadline.
            limit = min(limit, arguments.timeout - waited)
        try:
            data = _snapshot(context, via, limit)
        except bridge.BridgeError as exc:
            if not (exc.unfinished and arguments.timeout):
                return _bridge_failed(context, exc, "The work list could not be read")
            # Read-only: a check cut off by the deadline simply found nothing in time.
            console.finish()
            console.warn(
                f"No work an outside agent can take within {duration(arguments.timeout)}; "
                f"the last check was still running: {exc.url}"
            )
            return Outcome(
                Result.NO_WORK,
                EXIT_CODES[Result.NO_WORK],
                {"status": "none", "checks": checks, "frontier": data, "unfinished": exc.url},
            )
        # The same live checks reserve-next makes (dependencies, CI), so
        # this never reports work the reservation would then refuse.
        found: list[dict[str, object]] = []
        not_yet = ""
        for offer in bridge.takeable(data, abilities):
            if login is None and offer.get("action") == "review":
                login = context.github.whoami()
            if login is not None and claimops.own_review(context.github, offer, login):
                if not not_yet:
                    not_yet = (
                        f"{offer.get('target')}: you authored it, so GitHub refuses your review"
                    )
                continue
            live, why = external.live_now(
                context.github,
                str(offer.get("target")),
                str(offer.get("action")),
                str(offer.get("head_sha") or "") or None,
            )
            if live:
                found.append(offer)
            elif not not_yet:
                not_yet = f"{offer.get('target')}: {why}"
        if found:
            console.finish()
            console.ok(f"Work an outside agent can take ({len(found)})")
            _show_offers(console, found, limit=5)
            _show_bridge(console, data)
            if arguments.claim:
                target = str(found[0].get("target"))
                console.waiting(f"Claiming {target} and waiting for the controller")
                answer = claimops.claim(context.github, context.clock, target)
                console.finish()
                shown = _show_claim_answer(console, answer)
                shown.data = {**shown.data, "available": found, "checks": checks}
                return shown
            # Keep the agent's own scope, or reserve-next may pick other work.
            scope = "".join(f" --can {action}" for action in sorted(abilities))
            flag = " --via github" if via == "github" else ""
            console.line(
                f"Reserve it: baste reserve-next --repo {context.repo} --client AGENT{scope}{flag}"
            )
            return Outcome(
                Result.COMPLETED,
                EXIT_OK,
                {"status": "available", "available": found, "checks": checks, "frontier": data},
            )
        waited = (context.clock.now() - started).total_seconds()
        pause = float(interval)
        if arguments.timeout:
            # Wait out the whole timeout; the last pause may be shorter.
            pause = max(0.0, min(pause, arguments.timeout - waited))
        if data.get("authoritative_claim_available") is not True:
            reason = "lease authority unavailable"
        elif not_yet:
            reason = not_yet
        else:
            reason = "every offer is held or needs an action you did not list"
        console.waiting(f"No work yet ({reason}); checked {checks}x, next in {duration(pause)}")
        context.clock.sleep(pause)


def _external_via_github(context: Context, arguments: argparse.Namespace) -> dict[str, object]:
    verb = arguments.factory_command
    context.console.waiting(f"Running the claim workflow on GitHub ({verb})")
    found = bridge.claim(
        context.github,
        context.clock,
        operation=verb,
        client=arguments.client,
        claim_id=getattr(arguments, "claim_id", ""),
        evidence=getattr(arguments, "evidence", ""),
        abilities=frozenset(getattr(arguments, "can", None) or ()),
        expected=getattr(arguments, "target", ""),
        started=context.progress,
    )
    context.console.finish()
    return {**found.data, "bridge": found.summary()}


def cmd_external(context: Context, arguments: argparse.Namespace) -> Outcome:
    if getattr(arguments, "via", "direct") != "github":
        direct = _external_direct(context, arguments)
        return direct if isinstance(direct, Outcome) else _show_external(context, direct)
    if getattr(arguments, "advertised_at", ""):
        raise UsageError("--advertised-at is not supported with --via github.")
    # Outside the refusal handler: a GitHub failure before the request is sent
    # is "GitHub unavailable", and one after it is "unknown", never "refused".
    try:
        outcome = _external_via_github(context, arguments)
    except bridge.BridgeError as exc:
        # The run may have acted (a reservation may exist); say where to look.
        return _bridge_failed(context, exc, "The claim workflow gave no result")
    if outcome.get("result") in ("failed", "invalid", "github_unavailable"):
        # The workflow ran and refused, exactly as a direct call would.
        why = outcome.get("reason") or outcome.get("error") or outcome.get("status")
        context.console.fail(f"External claim refused: {why}")
        _show_bridge(context.console, outcome)
        return Outcome(Result.FAILED, 4, outcome)
    if not outcome.get("status"):
        context.console.fail("The claim workflow published no status")
        _show_bridge(context.console, outcome)
        return Outcome(Result.UNKNOWN, EXIT_CODES[Result.UNKNOWN], outcome)
    return _show_external(context, outcome)


def _external_direct(
    context: Context, arguments: argparse.Namespace
) -> dict[str, object] | Outcome:
    verb = arguments.factory_command
    try:
        if verb == "revoke":
            outcome = external.revoke(
                context.github,
                claim_id=arguments.claim_id,
                reason=arguments.reason,
                environment=dict(os.environ),
            )
        elif verb == "reserve-next":
            outcome = external.reserve_next(
                context.github,
                client=arguments.client,
                environment=dict(os.environ),
                abilities=frozenset(arguments.can) if arguments.can else external.ACTIONS,
                expected=arguments.target,
                advertised_at=arguments.advertised_at,
            )
        else:
            outcome = external.claim_action(
                context.github,
                client=arguments.client,
                claim_id=arguments.claim_id,
                operation=verb,
                environment=dict(os.environ),
                evidence=getattr(arguments, "evidence", ""),
            )
    except (RuntimeError, ValueError) as exc:
        context.console.fail(f"External claim refused: {exc}")
        return Outcome(
            Result.FAILED,
            4,
            {"status": "held", "reason": str(exc), "retry": "refresh_frontier"},
        )
    except Exception as exc:  # noqa: BLE001 - claim store or network failure
        # Database and network errors can carry connection details; report
        # only their type. Nothing was reserved unless the ledger said so.
        reason = f"claim store unavailable ({type(exc).__name__}); check leases before retrying"
        context.console.fail(f"External claim refused: {reason}")
        return Outcome(
            Result.FAILED,
            4,
            {"status": "held", "reason": reason, "retry": "check_lease_before_retry"},
        )
    return outcome


def _show_external(context: Context, outcome: dict[str, object]) -> Outcome:
    state = str(outcome.get("status"))
    if state in ("reserved", "renewed", "released", "revoked"):
        context.console.ok(f"External claim {state}")
        handoff = outcome.get("handoff")
        if isinstance(handoff, dict):
            context.console.line(f"  {handoff.get('target')}: {handoff.get('goal')}")
            context.console.line(f"  {handoff.get('url')}")
            context.console.line(f"  Lease: {handoff.get('claim_id')}")
            renew = handoff.get("renew_github" if "bridge" in outcome else "renew")
            context.console.line(f"  Renew: {renew}")
        _show_bridge(context.console, outcome)
        return Outcome(Result.COMPLETED, EXIT_OK, outcome)
    context.console.warn(f"External claim not acquired: {outcome.get('reason', state)}")
    _show_bridge(context.console, outcome)
    return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], outcome)
