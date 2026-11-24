"""Turn the raw reads behind ``factory recap`` into short, plain rows.

Everything here is pure: the same sources and clock give the same recap. Rows
carry ISO timestamps, never formatted times, so each front end shows its own
local time. Row text names items by title; the number is only a reference.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import Obj, integer, items, label_names, obj, string
from rotisserie.factory_console.service import db_usage, reliability
from rotisserie.factory_console.service.doctor import Finding
from rotisserie.factory_console.service.failures import (
    FAILED_CONCLUSIONS,
    WorkerFailure,
    real_cause,
)
from rotisserie.factory_console.service.fleet import FleetState
from rotisserie.factory_console.service.report import OUTCOME_WORDS, provider_rows
from rotisserie.factory_console.service.review import BLOCKED
from rotisserie.factory_console.service.self_heal import Heal
from rotisserie.factory_console.service.stalls import Stall
from rotisserie.factory_console.work import Item

CLAIMED = "factory:claimed"
CLAIM_REJECTED = "factory:claim-rejected"
RUNNING_TOO_LONG = timedelta(hours=2)
CLAIM_IDLE = timedelta(hours=1)
WASTED_RUNS = 3
CAUSE_WORDS = {**OUTCOME_WORDS, "no_work": "nothing it could do", "": "no recorded cause"}
FIXES = {
    "no_capacity": "Add a provider or turn one back on: no model was free to take the work.",
    "provider_quota": "Add credits, or turn the provider off until its quota resets.",
    "control_plane_failure": "Run `baste failures` to see which factory step broke.",
    "model_retired_410": "Turn the retired model off with `baste models disable`.",
    "unconfigured": "Add the provider's credentials, or turn the provider off.",
}
HANDLED_CHECKS = frozenset(("main verification", "review deadlocks"))
PREFIX = re.compile(r"^[A-Za-z]+(?:\([^)]*\))?!?:\s*")
SUFFIX = re.compile(r"\s*\((?:[\w./-]*)?#\d+\)\s*$")
FACTORY_SCOPE = re.compile(r"^(?:ci|[A-Za-z]+\([^)]*\b(?:factory|rotisserie|ci)\b[^)]*\))!?:")
FACTORY_WORD = re.compile(r"\b(?:factory|rotisserie)\b", re.I)


@dataclass
class Sources:
    since: datetime
    now: datetime
    merged: list[Obj] = field(default_factory=list)
    runs: list[Obj] = field(default_factory=list)
    failures: list[WorkerFailure] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    pulls: list[Obj] = field(default_factory=list)
    stalls: list[Stall] = field(default_factory=list)
    healed: list[Heal] = field(default_factory=list)
    claims: list[Obj] = field(default_factory=list)
    stats: Obj | None = None
    fleet: FleetState | None = None
    work: list[Item] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)


def plain_title(title: str) -> str:
    """Drop the commit-style prefix and trailing issue reference: the words a person reads."""
    text = SUFFIX.sub("", PREFIX.sub("", title.strip())).strip()
    return text[:1].upper() + text[1:] if text else title.strip()


def is_factory(title: str) -> bool:
    """A change to the factory itself, not to the product it builds."""
    return bool(FACTORY_SCOPE.match(title.strip()) or FACTORY_WORD.search(title))


def named(title: str, number: int | None) -> str:
    return f"{plain_title(title)} (#{number})" if number else plain_title(title)


def merged(sources: Sources) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = {"product": [], "factory": []}
    for pr in sources.merged:
        title = string(pr, "title")
        groups["factory" if is_factory(title) else "product"].append(
            {
                "number": integer(pr, "number"),
                "title": plain_title(title),
                "url": string(pr, "html_url"),
                "merged_at": string(obj(pr.get("pull_request")), "merged_at")
                or string(pr, "closed_at")
                or None,
            }
        )
    return groups


def main_state(findings: list[Finding]) -> str:
    found = next((f for f in findings if f.check == "main verification"), None)
    if found is None:
        return "unknown"
    return {"ok": "green", "fail": "failing"}.get(found.status, "unverified")


def _titles(pulls: list[Obj]) -> dict[int, Obj]:
    return {integer(pr, "number") or 0: pr for pr in pulls}


def needs_you(sources: Sources) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    def add(kind: str, text: str, action: str, url: str = "") -> None:
        rows.append({"kind": kind, "text": text, "action": action, "url": url or None})

    for finding in sources.findings:
        if finding.check == "main verification" and finding.status == "fail":
            add("main", f"Main is failing: {finding.text}", finding.hint or "Fix main first.")
        elif finding.status == "fail" and not finding.fix and finding.check not in HANDLED_CHECKS:
            add("doctor", f"{finding.check.capitalize()}: {finding.text}", finding.hint)
    pulls = _titles(sources.pulls)
    for stall in sources.stalls:
        if stall.auto:
            continue
        pr = pulls.get(stall.pr, {})
        add(
            "deadlock",
            f"{named(string(pr, 'title'), stall.pr)} is stuck in review. {stall.text}",
            stall.hint,
            stall.url,
        )
    for pr in sources.pulls:
        if {"factory", BLOCKED} <= label_names(pr):
            number = integer(pr, "number")
            add(
                "blocked",
                f"{named(string(pr, 'title'), number)} is parked as blocked.",
                f"`baste review pr:{number}` says why and can send it back.",
                string(pr, "html_url"),
            )
    for item in sources.claims:
        labels = label_names(item)
        number = integer(item, "number")
        kind = "pr" if item.get("pull_request") else "issue"
        name = named(string(item, "title"), number)
        if CLAIM_REJECTED in labels:
            add("claim", f"Your claim on {name} was refused.", "Read its status comment.")
        elif CLAIMED in labels:
            add(
                "claim",
                f"{name} has an open claim (factory:claimed).",
                f"When you are done, run `baste finish {kind}:{number}`.",
                string(item, "html_url"),
            )
    quota = Counter(row.provider or "unknown" for row in sources.failures if _out_of_quota(row))
    for provider, count in quota.most_common():
        add(
            "credits",
            f"{provider} ran out of quota or credits ({count} failed runs).",
            FIXES["provider_quota"],
        )
    return rows


def clearing(sources: Sources) -> list[dict[str, object]]:
    """Stalls the factory's next dispatcher pass clears, kept out of "Needs you"."""
    pulls = _titles(sources.pulls)
    return [
        {
            "text": f"{named(string(pulls.get(stall.pr, {}), 'title'), stall.pr)}: {stall.text}",
            "url": stall.url or None,
        }
        for stall in sources.stalls
        if stall.auto
    ]


def healed(sources: Sources) -> list[dict[str, object]]:
    """What the factory cleared by itself, one plain line each."""
    return [
        {"kind": heal.kind, "text": heal.text, "at": heal.at, "url": heal.url or None}
        for heal in sources.healed
    ]


def _out_of_quota(row: WorkerFailure) -> bool:
    return row.outcome_class == "provider_quota"


def went_wrong(sources: Sources) -> dict[str, object]:
    groups: dict[str, list[WorkerFailure]] = defaultdict(list)
    for row in sources.failures:
        groups[real_cause(row)].append(row)
    causes = []
    for name, rows in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        errors = Counter(row.error for row in rows if row.error)
        providers = Counter(row.provider for row in rows if row.provider)
        causes.append(
            {
                "cause": name or None,
                "words": CAUSE_WORDS.get(name, name.replace("_", " ")),
                "count": len(rows),
                "providers": [name for name, _ in providers.most_common(3)],
                "example": errors.most_common(1)[0][0][:160] if errors else None,
                "fix": FIXES.get(name),
            }
        )
    failed = sum(1 for run in sources.runs if string(run, "conclusion") in FAILED_CONCLUSIONS)
    return {"read": len(sources.failures), "failed_runs": failed, "causes": causes}


def providers(sources: Sources) -> dict[str, list[dict[str, object]]]:
    found: dict[str, list[dict[str, object]]] = {
        "delivered": [],
        "wasted": [],
        "off": [],
        "paused": [],
    }
    if sources.stats is not None:
        for name, runs, delivered, _done, _idle, failed in provider_rows(sources.stats, "24h"):
            if int(delivered):
                found["delivered"].append(
                    {"provider": name, "runs": int(runs), "delivered": int(delivered)}
                )
            elif int(failed) >= WASTED_RUNS:
                found["wasted"].append({"provider": name, "runs": int(runs), "failed": int(failed)})
        found["paused"] = _paused(sources.stats, sources.now)
    if sources.fleet is not None:
        offs = [*sources.fleet.providers.values(), *sources.fleet.models.values()]
        found["off"] = [off.to_dict() for off in offs]
    return found


def _paused(stats: Obj, now: datetime) -> list[dict[str, object]]:
    paused: dict[str, dict[str, object]] = {}
    for entry in items(stats, "models"):
        health = obj(entry.get("health"))
        if reliability.health_status(health, now) != "paused":
            continue
        name = runtime.provider_for_source(string(entry, "source"))
        row = paused.setdefault(name, {"provider": name, "models": 0, "until": None, "why": None})
        row["models"] = int(str(row["models"])) + 1
        until = string(health, "cooldown_until")
        if until and (row["until"] is None or until > str(row["until"])):
            row["until"] = until
        last = string(health, "last_outcome")
        row["why"] = row["why"] or (CAUSE_WORDS.get(last, last.replace("_", " ")) or None)
    return sorted(paused.values(), key=lambda row: str(row["provider"]))


def database(sources: Sources) -> dict[str, object] | None:
    """Turso usage from the published snapshot; nothing while it is not in use."""
    report = db_usage.from_stats(sources.stats)
    state = str(report.get("status"))
    if sources.stats is None or state in ("unpublished", "not_configured"):
        return None
    return {"status": state, "text": db_usage.summary(report)}


def age_words(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} minutes"
    hours = minutes // 60
    return f"{hours} hours" if hours < 48 else f"{hours // 24} days"


def stuck(sources: Sources) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in sources.work:
        name = named(item.title, item.number)
        if item.run is not None and item.run.started is not None:
            age = sources.now - item.run.started
            if age < RUNNING_TOO_LONG:
                continue
            text = f"{name} has had a worker on it for {age_words(age)}."
            moment = item.run.started
        elif item.run is None and item.updated is not None:
            age = sources.now - item.updated
            if age < CLAIM_IDLE:
                continue
            text = f"{name} is claimed, but no worker has run for {age_words(age)}."
            moment = item.updated
        else:
            continue
        rows.append({"ref": item.ref, "text": text, "url": item.url, "since": moment.isoformat()})
    return rows


def verdict(
    *,
    merged_count: int,
    main: str,
    scheduler_stopped: bool,
    capacity: int,
    failures_read: int,
    failed_runs: int,
    finished: int,
    succeeded: int,
    needs: int,
) -> tuple[str, str]:
    """(tone, sentence). The worst problem wins; then merges; then a quiet night."""
    prs = f"{merged_count} PR{'s' if merged_count != 1 else ''} merged"
    main_words = {
        "green": "main green",
        "failing": "main failing",
        "unverified": "main not verified yet",
    }.get(main, "main state unknown")
    if main == "failing":
        tone, text = "rough", f"Rough night: main is failing, {prs}"
    elif scheduler_stopped:
        tone, text = "rough", f"Rough night: the scheduler stopped, {prs}"
    elif (
        failures_read >= WASTED_RUNS and capacity * 2 > failures_read and failed_runs * 2 > finished
    ):
        tone, text = "rough", f"Rough night: most failed runs had no capacity, {prs}"
    elif merged_count == 0 and finished >= 5 and succeeded == 0:
        tone, text = "rough", "Rough night: nothing merged and no run succeeded"
    elif merged_count:
        tone, text = "good", f"Good night: {prs}, {main_words}"
    else:
        tone, text = "quiet", f"Quiet night: nothing merged, {main_words}"
    if needs:
        text += f". {needs} thing{'s' if needs != 1 else ''} need{'s' if needs == 1 else ''} you"
    return tone, text
