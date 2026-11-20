#!/usr/bin/env python3
"""Daily dispatch caps for metered free providers (OrcaRouter, Requesty).

Free OpenAI-compatible accounts publish small per-account daily request
limits. One factory session is many model requests, so the factory caps how
many Entry runs a whole *source* may start per UTC day. The count comes from
the Actions run history of ``free-model-factory-entry.yml`` (run titles are
``Factory N · fixed-model entry``), so no extra durable state is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

GITHUB_DIR = Path(__file__).resolve().parents[1]
BUDGETS_PATH = GITHUB_DIR / "factory-source-budgets.json"
ENTRY_TITLE_RE = re.compile(r"^Factory (?P<worker>\d+) · fixed-model entry$")
PROTECTED_WORKERS: frozenset[int] = frozenset()
# Cloudflare Workers Free hard-stops daily neuron usage. Enforce a conservative
# source cap here so this factory-only guard remains on the fast-merge allowlist.
HARD_FREE_SOURCE_DISPATCH_CAPS = {"cloudflare-workers-ai": 1}


def load_budgets(path: Path | str = BUDGETS_PATH) -> dict[str, int]:
    """Return ``{source: max_dispatches_per_utc_day}``; a missing file means no caps."""
    path = Path(path)
    if not path.exists():
        return dict(HARD_FREE_SOURCE_DISPATCH_CAPS)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported factory-source-budgets schema_version")
    caps: dict[str, int] = {}
    for source, entry in (data.get("sources") or {}).items():
        cap = entry.get("max_dispatches_per_utc_day")
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            raise ValueError(f"{source}: max_dispatches_per_utc_day must be a non-negative integer")
        caps[str(source)] = cap
    return {**caps, **HARD_FREE_SOURCE_DISPATCH_CAPS}


def worker_sources(path: Path | str | None = None) -> dict[str, str]:
    """Derive provider identity from registered slots, never production model pins.

    An explicit fixture path remains supported for isolated historical parsers.
    """
    if path is None:
        from factory_worker_slots import slots

        return {slot.worker: slot.source for slot in slots()}
    mapping: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        fields = raw.split("\t")
        if len(fields) >= 2 and fields[0].strip().isdecimal():
            mapping[fields[0].strip()] = fields[1].strip()
    return mapping


def count_dispatches(
    titles: Iterable[str], sources: Mapping[str, str], budgeted: Iterable[str]
) -> dict[str, int]:
    """Count today's Entry runs per budgeted source from run titles."""
    wanted = set(budgeted)
    counts = dict.fromkeys(wanted, 0)
    for title in titles:
        match = ENTRY_TITLE_RE.match(title or "")
        if not match:
            continue
        source = sources.get(match.group("worker"))
        if source in wanted:
            counts[source] += 1
    return counts


def todays_entry_titles(now: datetime | None = None) -> list[str]:
    """Return Entry run titles created since 00:00 UTC today (via gh api)."""
    now = now or datetime.now(timezone.utc)
    repo = os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
    day = now.strftime("%Y-%m-%d")
    output = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/actions/workflows/free-model-factory-entry.yml/runs"
            f"?created=%3E%3D{day}&per_page=100",
            "--jq",
            ".workflow_runs[].display_title",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    return [line for line in output.splitlines() if line.strip()]


def exhausted_sources(
    titles: Iterable[str] | None = None,
    *,
    caps: Mapping[str, int] | None = None,
    sources: Mapping[str, str] | None = None,
) -> dict[str, tuple[int, int]]:
    """Return ``{source: (used, cap)}`` for budgeted sources at or over cap today."""
    caps = load_budgets() if caps is None else caps
    if not caps:
        return {}
    sources = worker_sources() if sources is None else sources
    if not any(source in caps for source in sources.values()):
        return {}
    counts = None
    if titles is None:
        try:
            from factory_state_operations import daily_usage
            counts = daily_usage(os.environ.get("GITHUB_REPOSITORY","JoshCLWren/comic-pile"),list(caps))
        except Exception:
            counts = None
    if counts is None:
        titles = todays_entry_titles() if titles is None else titles
        counts = count_dispatches(titles, sources, caps)
    return {source: (used, caps[source]) for source, used in counts.items() if used >= caps[source]}


def budget_block_reason(
    worker: str,
    *,
    titles: Iterable[str] | None = None,
    caps: Mapping[str, int] | None = None,
    sources: Mapping[str, str] | None = None,
) -> str | None:
    """Return why a worker's source is out of daily dispatch budget, else None."""
    if not str(worker).isdigit() or int(worker) in PROTECTED_WORKERS:
        return None
    sources = worker_sources() if sources is None else sources
    caps = load_budgets() if caps is None else caps
    source = sources.get(str(worker))
    if source is None or source not in caps:
        return None
    try:
        exhausted = exhausted_sources(titles, caps=caps, sources=sources)
    except (OSError, subprocess.SubprocessError) as exc:
        # Fail closed for metered sources only: an unreadable run history must
        # not risk blowing a small account budget.
        return f"Factory {worker} source {source} budget could not be verified ({exc.__class__.__name__})"
    if source not in exhausted:
        return None
    used, cap = exhausted[source]
    return f"Factory {worker} source {source} reached its daily dispatch budget ({used}/{cap} UTC today)"


def filter_workers(
    workers: list[str],
    *,
    titles: Iterable[str] | None = None,
    caps: Mapping[str, int] | None = None,
    sources: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Drop workers whose source budget is exhausted; also cap within this batch."""
    caps = load_budgets() if caps is None else caps
    if not caps:
        return list(workers), []
    sources = worker_sources() if sources is None else sources
    counts = None
    if titles is None and any(sources.get(str(w)) in caps for w in workers):
        try:
            from factory_state_operations import daily_usage
            counts = daily_usage(os.environ.get("GITHUB_REPOSITORY","JoshCLWren/comic-pile"),list(caps))
        except Exception:
            counts = None
        if counts is None:
            try:
                titles = todays_entry_titles()
            except (OSError, subprocess.SubprocessError) as exc:
                kept = [w for w in workers if sources.get(str(w)) not in caps]
                dropped = [w for w in workers if sources.get(str(w)) in caps]
                return kept, [
                    f"Factory {w} skipped: metered source budget could not be verified ({exc.__class__.__name__})"
                    for w in dropped
                ]
    if counts is None:
        counts = count_dispatches(titles or [], sources, caps)
    kept: list[str] = []
    messages: list[str] = []
    for worker in workers:
        source = sources.get(str(worker))
        if source is not None and source in caps and int(str(worker)) not in PROTECTED_WORKERS:
            if counts[source] >= caps[source]:
                messages.append(
                    f"Factory {worker} skipped: source {source} daily dispatch budget "
                    f"{counts[source]}/{caps[source]} reached (UTC day)"
                )
                continue
            counts[source] += 1
        kept.append(worker)
    return kept, messages


def main(argv: list[str] | None = None) -> int:
    """CLI: ``filter`` reads a JSON worker array on stdin; ``status`` prints usage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("filter", "status", "validate"))
    args = parser.parse_args(argv)
    if args.command == "validate":
        load_budgets()
        return 0
    if args.command == "status":
        caps = load_budgets()
        try:
            from factory_state_operations import daily_usage
            counts = daily_usage(os.environ.get("GITHUB_REPOSITORY","JoshCLWren/comic-pile"),list(caps))
        except Exception:
            counts = None
        if counts is None:
            counts = count_dispatches(todays_entry_titles(), worker_sources(), caps)
        print(json.dumps({s: {"used": counts[s], "cap": caps[s]} for s in caps}))
        return 0
    workers = [str(item) for item in json.load(sys.stdin)]
    kept, messages = filter_workers(workers)
    for message in messages:
        print(message, file=sys.stderr)
    print(json.dumps(kept, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
