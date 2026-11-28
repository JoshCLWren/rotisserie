"""Pure estimate math: factory attempts from markers, similarity features, ranges.

An item is one linked issue with its pull requests, or a pull request with
no linked issue. Attempts are worker leases (controller-claim markers). The
controller writes the same lease on an issue and its PR a few seconds apart,
so one worker's claims less than a minute apart count once.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import _rotisserie_native
from rotisserie.factory_console.service.estimate_history import Thread

INTAKE_LABELS = ("bug", "user-reported", "code-debt", "frontend")
SIZES = ((1500, "small"), (4000, "medium"))
OWNER_LABELS = frozenset(("factory:claimed", "factory:local"))
BLOCKED_LABEL = "factory:blocked"
MIRROR_SECONDS = 60
Z80 = 1.2816
MIN_SIMILAR = 6
TIERS = (
    ("same start, category, labels and size", ("origin", "category", "labels", "size")),
    ("same start, category and labels", ("origin", "category", "labels")),
    ("same start and category", ("origin", "category")),
)
HUMAN_SIGNALS = {
    "merged_by_person": "a person merged it",
    "owner_claim": "the owner claimed it (factory:claimed or factory:local)",
    "blocked": "it was parked in factory:blocked",
    "person_wrote": "a person wrote the change (author marker says model-human)",
    "operator_revoked": "an operator revoked a claim on it (store history only)",
}


def claim_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(
        rf"<!--\s*{re.escape(prefix)}-factory-controller-claim-v1:"
        r"(?:issue|pr)-\d+:([^:\s]+):(\d{10})"
    )


def author_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"<!--\s*{re.escape(prefix)}-agent-model-author-v1:[^>]*model-human")


def claims(threads: Iterable[Thread], pattern: re.Pattern[str]) -> list[int]:
    """Lease start times (epoch seconds), with mirrored issue/PR claims counted once."""
    raw = sorted(
        (int(match.group(2)), match.group(1))
        for thread in threads
        for comment in thread.comments
        if comment.bot or "[bot]" in comment.author or comment.author == "github-actions"
        for match in pattern.finditer(comment.body)
    )
    kept: list[int] = []
    last: dict[str, int] = {}
    for epoch, worker in raw:
        if worker in last and epoch - last[worker] < MIRROR_SECONDS:
            continue
        last[worker] = epoch
        kept.append(epoch)
    return kept


def size_bucket(chars: int) -> str:
    return next((name for limit, name in SIZES if chars < limit), "large")


@dataclass(frozen=True)
class Features:
    origin: str
    category: str
    labels: tuple[str, ...]
    size: str
    body_chars: int

    def value(self, name: str) -> object:
        return self.to_dict()[name]

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "category": self.category,
            "labels": self.labels,
            "size": self.size,
            "body_chars": self.body_chars,
        }


def features(source: Thread, category: Callable[[set[str]], str]) -> Features:
    """Intake facts only: stage, approval and via: labels would leak the outcome.

    ``origin`` separates factory work that starts from an issue from a
    standalone pull request, which is usually a person's change the factory reviews.
    """
    flags = tuple(name for name in INTAKE_LABELS if name in source.labels)
    return Features(
        source.kind,
        category(set(source.labels)),
        flags,
        size_bucket(source.body_size),
        source.body_size,
    )


@dataclass(frozen=True)
class Item:
    key: str
    numbers: tuple[int, ...]
    features: Features
    outcome: str  # merged | closed | open
    attempts: int
    first_claim: datetime | None
    pr_opened: datetime | None
    merged_at: datetime | None
    finished_at: datetime | None
    signals: tuple[str, ...]
    providers: tuple[str, ...] = field(default=())
    labels_incomplete: bool = False
    created_at: datetime | None = None

    @property
    def unattended(self) -> bool:
        return self.outcome == "merged" and not self.signals

    def hours(self, clock: str) -> float | None:
        start = (self.first_claim or self.pr_opened) if clock == "claim" else self.pr_opened
        if self.merged_at is None or start is None:
            return None
        return max(0.0, (self.merged_at - start).total_seconds() / 3600)


def build_item(
    key: str,
    source: Thread,
    pulls: Sequence[Thread],
    issue: Thread | None,
    *,
    claim: re.Pattern[str],
    author: re.Pattern[str],
    category: Callable[[set[str]], str],
) -> Item:
    every = ([issue] if issue else []) + list(pulls)
    starts = claims(every, claim)
    merged = [pr for pr in pulls if pr.merged is not None]
    winner = max(merged, key=lambda pr: pr.merged or datetime.min.replace(tzinfo=UTC), default=None)
    if winner is not None:
        outcome = "merged"
    elif issue is not None and (issue.state == "open" or issue.reason == "completed"):
        outcome = "open" if issue.state == "open" else "elsewhere"
    else:
        outcome = "closed"
    labels = set().union(*(thread.labels | thread.labeled for thread in every))
    signals = []
    if winner is not None and winner.merged_by_bot is False:
        signals.append("merged_by_person")
    if labels & OWNER_LABELS:
        signals.append("owner_claim")
    if BLOCKED_LABEL in labels:
        signals.append("blocked")
    if any(author.search(c.body) for pr in pulls for c in pr.comments):
        signals.append("person_wrote")
    closes = [pr.closed for pr in pulls if pr.closed is not None]
    providers = sorted({name[4:] for name in labels if name.startswith("via:")})
    return Item(
        key=key,
        numbers=tuple(sorted(thread.number for thread in every)),
        features=features(source, category),
        outcome=outcome,
        attempts=len(starts),
        first_claim=datetime.fromtimestamp(starts[0], UTC) if starts else None,
        pr_opened=winner.created if winner else None,
        merged_at=winner.merged if winner else None,
        finished_at=winner.merged if winner else max(closes, default=None),
        signals=tuple(signals),
        providers=tuple(providers),
        labels_incomplete=any(thread.labels_incomplete for thread in every),
        created_at=min((t.created for t in every if t.created), default=source.created),
    )


def match(
    target: Features, items: Sequence[Item]
) -> tuple[str, list[str], list[Item], list[dict[str, object]]]:
    """The narrowest tier with enough resolved look-alikes, and how many each tier had."""
    resolved = [item for item in items if item.outcome in ("merged", "closed")]
    tried: list[dict[str, object]] = []
    for name, used in TIERS:
        found = [i for i in resolved if all(i.features.value(f) == target.value(f) for f in used)]
        tried.append({"tier": name, "features": list(used), "count": len(found)})
        if len(found) >= MIN_SIMILAR:
            return name, list(used), found, tried
    return "", [], [], tried


def spread(values: Sequence[float]) -> dict[str, object] | None:
    """Median and the middle 80% (10th to 90th percentile), or None with too few values."""
    return _rotisserie_native.spread(values, MIN_SIMILAR)


def wilson(successes: int, total: int, z: float = Z80) -> tuple[float, float]:
    """80% Wilson score interval for a proportion; stays inside 0..1 with small samples."""
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def chance(similar: Sequence[Item]) -> dict[str, object] | None:
    if len(similar) < MIN_SIMILAR:
        return None
    wins = sum(item.unattended for item in similar)
    low, high = wilson(wins, len(similar))
    signals = Counter(signal for item in similar for signal in item.signals)
    return {
        "probability": wins / len(similar),
        "low": low,
        "high": high,
        "unattended": wins,
        "n": len(similar),
        "merged": sum(item.outcome == "merged" for item in similar),
        "human_signals": dict(signals),
        "label_history_incomplete": sum(item.labels_incomplete for item in similar),
    }
