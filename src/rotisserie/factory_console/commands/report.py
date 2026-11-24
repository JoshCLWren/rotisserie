from __future__ import annotations

import argparse
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import (
    EXIT_OK,
    OUTCOME_WORDS,
    STATS_BRANCH,
    Outcome,
)
from rotisserie.factory_console.commands.settings import _health_word
from rotisserie.factory_console.commands.status import parse_since
from rotisserie.factory_console.context import Context
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    integer,
    items,
    obj,
    string,
    timestamp,
)
from rotisserie.factory_console.service import reliability
from rotisserie.factory_console.service.failures import runs_since
from rotisserie.factory_console.tracker import (
    DISPATCH_WORKFLOW,
    EXIT_CODES,
    WORKER_WORKFLOW,
    Result,
    Tracker,
)
from rotisserie.factory_console.ui import duration, local_time, since
from rotisserie.factory_console.work import current_work, one_per_worker


def cmd_report(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    github = context.github
    now = context.clock.now()
    start = parse_since(arguments.since, now)
    iso = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    console.heading(
        f"Factory report since {local_time(start)} ({duration((now - start).total_seconds())} ago)"
    )
    data: dict[str, object] = {"since": start.isoformat()}

    # Is it running at all?
    dispatches = items(
        github.repo_api(f"actions/workflows/{DISPATCH_WORKFLOW}/runs?per_page=1"), "workflow_runs"
    )
    last_dispatch = timestamp(dispatches[0], "created_at") if dispatches else None
    workers = runs_since(github, WORKER_WORKFLOW, iso)
    coding_now = one_per_worker(current_work(github, with_session=False))
    console.line(console.bold("Is it running?"))
    if last_dispatch is None or now - last_dispatch > timedelta(minutes=20):
        console.fail(
            "The scheduler has not run recently", f"last run {since(last_dispatch, now)} ago"
        )
    else:
        console.ok("Scheduler is running", f"last run {since(last_dispatch, now)} ago")
    console.note(
        f"{len(coding_now)} item(s) being worked on now  (details: rotisserie factory status)"
    )
    data["scheduler_last_run"] = last_dispatch.isoformat() if last_dispatch else None
    data["working_now"] = len(coding_now)

    # What shipped?
    merged = _search(github, f"repo:{context.repo} is:pr is:merged merged:>={iso}")
    opened = _search(github, f"repo:{context.repo} is:pr created:>={iso}")
    console.line()
    console.line(console.bold(f"Merged ({len(merged)})"))
    for pr in merged[:25]:
        console.line(
            f"  {console.green(console.ok_mark)} #{integer(pr, 'number')}  "
            f"{string(pr, 'title')[:80]}"
        )
    if not merged:
        console.line("  Nothing merged.")
    open_now = [pr for pr in opened if string(pr, "state") == "open"]
    console.note(f"{len(opened)} pull request(s) opened; {len(open_now)} still open")
    data["merged"] = [
        {"number": integer(pr, "number"), "title": string(pr, "title")} for pr in merged
    ]
    data["opened"] = len(opened)

    # How did the workers do?
    conclusions = Counter(string(run, "conclusion") or string(run, "status") for run in workers)
    console.line()
    console.line(console.bold(f"Workers ({len(workers)} runs)"))
    console.note(
        ", ".join(f"{count} {_conclusion_word(name)}" for name, count in conclusions.most_common())
        or "No worker runs."
    )
    data["worker_runs"] = dict(conclusions)

    # What is broken?
    failed = [run for run in workers if string(run, "conclusion") == "failure"][:15]
    capacity, problems = split_capacity(_failure_reasons(github, failed))
    console.line()
    console.line(console.bold("Where it is breaking"))
    if failed:
        console.note(f"from the {len(failed)} most recent failed worker runs")
    for (reason, provider), count in capacity.most_common(5):
        console.line(
            f"  {console.yellow(console.warn_mark)} {count}×  {reason}"
            + (f"  {console.dim('(' + provider + ')')}" if provider else "")
        )
    data["no_capacity"] = [
        {"reason": reason, "provider": provider, "count": count}
        for (reason, provider), count in capacity.most_common(20)
    ]
    if problems:
        for (reason, provider), count in problems.most_common(8):
            console.line(
                f"  {console.red(console.fail_mark)} {count}×  {reason}"
                + (f"  {console.dim('(' + provider + ')')}" if provider else "")
            )
    else:
        console.line("  No worker failures with a recorded reason.")
    data["failures"] = [
        {"reason": reason, "provider": provider, "count": count}
        for (reason, provider), count in problems.most_common(20)
    ]
    main_state = _main_health(github)
    if main_state:
        console.fail(f"main has failing checks: {', '.join(main_state[:5])}")
    else:
        console.ok("main is green")
    data["main_failing_checks"] = main_state
    stats = _load_stats(github)
    if stats is not None:
        rows = _provider_rows(stats, "24h")
        bad = [row for row in rows if row[2] == "0" and int(row[1]) >= 3]
        for row in bad:
            console.warn(f"{row[0]}: {row[1]} runs in 24h, none delivered")
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _conclusion_word(name: str) -> str:
    return {
        "success": "finished",
        "failure": "failed",
        "cancelled": "stopped",
        "in_progress": "running",
        "queued": "waiting",
        "skipped": "skipped",
    }.get(name, name)


def _search(github: GitHub, query: str) -> list[Obj]:
    from urllib.parse import quote

    return items(github.api(f"search/issues?q={quote(query)}&per_page=100&sort=updated"), "items")


def _failure_reasons(github: GitHub, runs: list[Obj]) -> Counter[tuple[str, str]]:
    tracker = Tracker(github)

    def reasons(run: Obj) -> list[tuple[str, str]]:
        run_id = integer(run, "id")
        if run_id is None:
            return []
        title = re.match(r"^Factory (\d+) ", string(run, "display_title"))
        provider = runtime.provider_for_worker(title.group(1)) if title else None
        try:
            outcome = tracker.worker_outcome(tracker.jobs(run_id))
        except GitHubError:
            return []
        return [(plain_failure(outcome.message), provider.name if provider else "")]

    counter: Counter[tuple[str, str]] = Counter()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for found in pool.map(reasons, runs):
            counter.update(found)
    return counter


NO_CAPACITY = "no capacity: "


def split_capacity(
    found: Counter[tuple[str, str]],
) -> tuple[Counter[tuple[str, str]], Counter[tuple[str, str]]]:
    """Capacity exits apart from real failures: (no capacity, failures)."""
    capacity = Counter({key: n for key, n in found.items() if key[0].startswith(NO_CAPACITY)})
    return capacity, found - capacity


def plain_failure(message: str) -> str:
    """Turn the factory's outcome sentence into a short, plain reason."""
    if message.startswith("No capacity: "):
        return NO_CAPACITY + message.removeprefix("No capacity: ")[:120]
    match = re.search(r"worker failed: ([a-z_0-9]+)", message)
    if match is None:
        return message[:120]
    reason = OUTCOME_WORDS.get(match.group(1), match.group(1).replace("_", " "))
    if message.startswith("Code saved"):
        reason += "; its code was saved on the pull request"
    return reason


def _main_health(github: GitHub) -> list[str]:
    try:
        repo = obj(github.repo_api(""))
        branch = string(repo, "default_branch") or "main"
        checks = items(github.repo_api(f"commits/{branch}/check-runs?per_page=100"), "check_runs")
    except GitHubError:
        return []
    return sorted(
        {
            string(check, "name")
            for check in checks
            if string(check, "conclusion") in ("failure", "timed_out", "startup_failure")
        }
    )


_load_stats = reliability.load_stats
_window = reliability.window
_count = reliability.count


def _provider_rows(stats: Obj, window: str) -> list[list[str]]:
    rows = []
    for entry in items(stats, "providers"):
        counts = _window(entry, window)
        provider = runtime.provider_for_source(string(entry, "source"))
        rows.append(
            [
                provider,
                str(_count(counts, "runs")),
                str(_count(counts, "delivered")),
                str(_count(counts, "completed") - _count(counts, "delivered")),
                str(_count(counts, "idle")),
                str(_count(counts, "failed")),
            ]
        )
    return sorted(rows, key=lambda row: (-int(row[2]), -int(row[1])))


def cmd_stats(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    stats = _load_stats(context.github)
    if stats is None:
        console.fail("No reliability snapshot has been published yet")
        console.line(
            "  The dashboard job publishes factory-stats.json to the "
            f"{STATS_BRANCH} branch every 15 minutes."
        )
        return Outcome(Result.UNKNOWN, EXIT_CODES[Result.UNKNOWN], {})
    generated = timestamp(stats, "generated_at")
    window = arguments.window
    words = {"24h": "last 24 hours", "7d": "last 7 days"}[window]
    console.heading(f"Reliability, {words}")
    console.note(
        f"Counts of finished worker runs, updated {since(generated, context.clock.now())} ago"
    )
    console.line()
    console.line(console.bold("Providers"))
    console.table(
        ["Provider", "Runs", "PRs", "Done", "Idle", "Failed"], _provider_rows(stats, window)
    )
    console.line()
    console.line(console.bold("Models"))
    rows = []
    models = []
    for entry in items(stats, "models"):
        source = string(entry, "source")
        if arguments.provider and runtime.resolve_provider(arguments.provider).source != source:
            continue
        counts = _window(entry, window)
        runs = _count(counts, "runs")
        health = obj(entry.get("health"))
        status = reliability.health_status(health, context.clock.now())
        if not runs and status not in ("paused", "retired"):
            continue
        failures = obj(counts.get("failures"))
        top = (
            max(failures.items(), key=lambda pair: pair[1] if isinstance(pair[1], int) else 0)[0]
            if failures
            else ""
        )
        delivered = _count(counts, "delivered")
        rows.append(
            [
                string(entry, "model")[:40],
                runtime.provider_for_source(source),
                str(runs),
                str(delivered),
                str(_count(counts, "failed")),
                OUTCOME_WORDS.get(top, top.replace("_", " ")),
                _health_word(health, context.clock.now()),
            ]
        )
        models.append(
            {
                "source": source,
                "model": string(entry, "model"),
                "runs": runs,
                "delivered": delivered,
                "failed": _count(counts, "failed"),
                "top_failure": top or None,
                "health": status or None,
            }
        )
    rows.sort(key=lambda row: (-int(row[3]), int(row[4]), -int(row[2])))
    console.table(
        ["Model", "Provider", "Runs", "PRs", "Failed", "Most common failure", "Now"], rows
    )
    if not rows:
        console.line("  No model ran in this window.")
    return Outcome(
        Result.COMPLETED,
        EXIT_OK,
        {
            "window": window,
            "generated_at": generated.isoformat() if generated else None,
            "models": models,
        },
    )
