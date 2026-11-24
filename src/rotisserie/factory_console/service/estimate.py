"""How long, how many attempts and how likely unattended: one item, from similar past items.

Reads GitHub (the recent closed ``factory`` PRs, their linked issues and
the factory's marker comments) and the published ``factory-stats.json``. With
``history`` (store counts the label claim workflow published), attempts and
person signals come from the factory store for every item the history window covers. It
never connects to the factory store. When too few similar items exist it says so and gives
no number.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, items
from rotisserie.factory_console.service import (
    claimops,
    estimate_outcomes,
    estimate_source,
    reliability,
)
from rotisserie.factory_console.service import estimate_history as source
from rotisserie.factory_console.service import estimate_model as model
from rotisserie.factory_console.service.estimate_history import Budget, Thread
from rotisserie.factory_console.service.estimate_source import History, Runs

DEFAULT_SAMPLE = 100
SAMPLE_RANGE = (10, 300)
CLOCKS = {
    "claim": "from the first factory claim on the issue or its PRs to the merge",
    "pr": "from the merged pull request's creation to the merge",
}
INCLUSION = (
    "closed PRs labelled factory that a factory worker touched (a controller-claim "
    "marker or a via: label), grouped with their linked issue"
)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _groups(pulls: list[Thread]) -> dict[str, list[Thread]]:
    grouped: dict[str, list[Thread]] = defaultdict(list)
    for pr in pulls:
        grouped[f"issue:{pr.linked}" if pr.linked else f"pr:{pr.number}"].append(pr)
    return grouped


def _factory_record(github: GitHub) -> dict[str, object] | None:
    stats = reliability.load_stats(github)
    if stats is None:
        return None
    runs = delivered = 0
    for entry in items(stats, "models"):
        counts = reliability.window(entry, "7d")
        runs += reliability.count(counts, "runs")
        delivered += reliability.count(counts, "delivered")
    return {"window": "7d", "runs": runs, "delivered": delivered}


def _source(
    history: History | None, fallback: str, covered: int, uncovered: int, target: Runs | None
) -> dict[str, object]:
    if history is None:
        return {"attempts": "github", "label": estimate_source.GITHUB, "fallback": fallback}
    return {
        "attempts": "neon",
        "label": estimate_source.NEON,
        "window_days": history.window_days,
        "covered_from": history.covered_from.isoformat(),
        "rows_read": history.rows_read,
        "truncated": history.truncated,
        "items_from_neon": covered,
        "items_from_github": uncovered,
        "target_from_neon": target is not None,
        "unknown_classes": history.unknown_classes,
        "mapping": estimate_outcomes.mapping(),
        "bridge": history.bridge,
    }


def estimate(
    github: GitHub,
    target: str,
    *,
    sample: int = DEFAULT_SAMPLE,
    now: datetime | None = None,
    history: History | None = None,
    fallback: str = "",
) -> dict[str, object]:
    kind, number = claimops.parse_target(target)
    low, high = SAMPLE_RANGE
    if not low <= sample <= high:
        raise ValueError(f"--sample must be between {low} and {high}")
    now = now or datetime.now(UTC)
    runtime.bind_repository(github.repo)
    prefix = str(runtime.module("factory_adopter").marker_prefix())
    policy = runtime.module("factory_work_policy")

    def category(labels: set[str]) -> str:
        return str(policy.work_category(labels, pull_request=False))

    patterns = {"claim": model.claim_pattern(prefix), "author": model.author_pattern(prefix)}
    budget = Budget()
    pulls, short = source.recent_pulls(github, budget, sample)

    found = source.threads(github, budget, [number])
    if number not in found:
        raise ValueError(f"{target} was not found in {github.repo}")
    subject = found[number]
    if (subject.kind == "pr") != (kind == "pr"):
        raise ValueError(f"#{number} is {'a pull request' if subject.kind == 'pr' else 'an issue'}")
    linked = subject.linked
    target_issue = subject if kind == "issue" else None
    if linked:
        target_issue = source.threads(github, budget, [linked]).get(linked)
    target_key = f"issue:{target_issue.number}" if target_issue else f"pr:{number}"

    grouped = _groups(pulls)
    grouped.pop(target_key, None)
    issues = source.threads(
        github, budget, [int(key[6:]) for key in grouped if key.startswith("issue:")]
    )
    past: list[model.Item] = []
    covered = refusals = 0
    for key, prs in grouped.items():
        issue = issues.get(int(key[6:])) if key.startswith("issue:") else None
        if issue is not None and issue.kind != "issue":
            issue = None
        item = model.build_item(key, issue or prs[0], prs, issue, category=category, **patterns)
        touched = item.attempts or item.providers
        runs = None
        if history is not None:
            item, runs = estimate_source.apply(item, history)
        if touched or (runs is not None and (runs.attempts or runs.assignments)):
            past.append(item)
            covered += runs is not None
            refusals += runs.refusals if runs else 0

    target_pulls = [subject] if kind == "pr" else []
    mine = model.build_item(
        target_key,
        target_issue or subject,
        target_pulls,
        target_issue,
        category=category,
        **patterns,
    )
    mine_runs = None
    if history is not None:
        mine, mine_runs = estimate_source.apply(mine, history)
    tier, used, similar, tried = model.match(mine.features, past)
    clock = "pr" if kind == "pr" else "claim"
    merged = [item for item in similar if item.outcome == "merged"]
    hours = [h for item in merged if (h := item.hours(clock)) is not None]
    finishes = [item.finished_at for item in past if item.finished_at]
    notes: list[str] = []
    if not similar:
        best = max((int(str(row["count"])) for row in tried), default=0)
        notes.append(
            f"Too few similar finished items: the closest group has {best}, and an estimate "
            f"needs at least {model.MIN_SIMILAR}. No numbers are given."
        )
    elif len(merged) < model.MIN_SIMILAR:
        notes.append(
            f"Only {len(merged)} similar items merged, too few for attempts and time "
            f"(need {model.MIN_SIMILAR})."
        )
    outcomes = Counter(item.outcome for item in past)
    if similar:
        notes.append(
            f"Not counted: {outcomes['open']} item(s) still open and {outcomes['elsewhere']} "
            "issue(s) completed by a PR outside the sample, so the chance is optimistic."
        )
    gaps = sum(item.labels_incomplete for item in similar)
    if gaps:
        missed = "factory:blocked" if history else "an owner claim or factory:blocked"
        notes.append(
            f"{gaps} similar item(s) have over 200 label changes; GitHub shows only the first "
            f"and last 100, so {missed} in between can be missed."
        )
    if short:
        notes.append("The GraphQL budget ran low, so fewer items were read than asked.")
    if fallback:
        notes.append(f"store history was not used ({fallback}); attempts come from GitHub markers.")
    if history is not None and len(past) - covered:
        notes.append(
            f"{len(past) - covered} item(s) have no store run in the history window (they "
            "started earlier, or no run was recorded), so their attempts come from GitHub markers."
        )
    if history is not None and history.unknown_classes:
        names = ", ".join(sorted(history.unknown_classes))
        notes.append(f"Outcome classes this console does not know count as attempts: {names}.")
    start = mine.first_claim if clock == "claim" else subject.created
    return {
        "target": target,
        "state": subject.state,
        "linked_issue": linked,
        "features": mine.features.to_dict(),
        "so_far": {
            "attempts": mine.attempts,
            "started_at": _iso(start),
            "elapsed_hours": (now - start).total_seconds() / 3600 if start else None,
            "providers": list(mine.providers),
            "refusals": mine_runs.refusals if mine_runs else None,
            "capacity_runs": mine_runs.capacity_runs if mine_runs else None,
            "assignments": mine_runs.assignments if mine_runs else None,
        },
        "source": _source(history, fallback, covered, len(past) - covered, mine_runs),
        "sample": {
            "requested": sample,
            "scanned_prs": len(pulls),
            "kept_items": len(past),
            "finished_items": outcomes["merged"] + outcomes["closed"],
            "outcomes": dict(outcomes),
            "rule": INCLUSION,
            "oldest_finish": _iso(min(finishes, default=None)),
            "newest_finish": _iso(max(finishes, default=None)),
            "cut_short": short,
            "neon_refusals": refusals if history is not None else None,
        },
        "similar": {
            "tier": tier,
            "features": used,
            "tried": tried,
            "count": len(similar),
            "items": [item.key for item in similar],
            "minimum": model.MIN_SIMILAR,
        },
        "attempts": model.spread([float(item.attempts) for item in merged]),
        "hours": model.spread(hours),
        "clock": {"name": clock, "meaning": CLOCKS[clock]},
        "unattended": model.chance(similar),
        "human_signals": model.HUMAN_SIGNALS,
        "factory_record": _factory_record(github),
        "budget": budget.to_dict(),
        "notes": notes,
    }
