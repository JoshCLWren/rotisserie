#!/usr/bin/env python3
"""Weight an `auto` pick inside the eligible live-catalog set by track record.

Data: the published ``factory-stats.json`` (trusted worker outcomes over seven
days, refreshed hourly by the status job). The run job reads it once from the
``factory-status-live`` branch and names the file in
``FACTORY_TRACK_RECORD_FILE``, so a pick costs no factory store reads.

Rule, per provider, over the models that are already eligible:

* A trial is a completed or failed run. Idle runs and non-model failures
  (control plane, capacity, runner environment) are not trials.
* Success is a delivered PR for ``implement`` and a completed run for every
  other action. Each model is judged on its runs of the same action kind
  when it has ``MIN_TRIALS`` of them there, else on all its runs.
* Models with fewer than ``MIN_TRIALS`` trials, or with no success yet, are
  untried. ``EXPLORE_SHARE`` of picks go to them uniformly, so new catalog
  models get tried.
* A model with ``PROVEN_BAD_TRIALS`` or more trials and a success rate under
  ``PROVEN_BAD_RATE`` is proven bad and is skipped for that action. The rule
  is absolute: a provider with few models does not lift a bad one. When every
  eligible model is proven bad, the pick fails so the caller moves on to
  another provider. An owner-pinned provider (``REQUESTED_PROVIDER``) keeps
  every model, because the owner asked for that provider.
* The other picks are Thompson sampling over the tried models: Beta posteriors
  around the fleet-wide success rate (every provider, same action), with
  evidence capped at ``EVIDENCE_CAP`` trials. The win probabilities are capped
  at ``MAX_SHARE`` so concurrent workers spread; each worker samples with its
  own hash. A hot model (``hot``) may take up to ``HOT_MAX_SHARE``: about
  68% of all picks while untried models exist (0.8 x 0.85), 85% when none do,
  and the other tried models keep at least 15%.
* Hot: ``HOT_MIN_TRIALS`` or more trials, and the Wilson lower bound of the
  success rate (90% one sided) at least ``HOT_LIFT`` times the fleet rate and
  at least ``HOT_FLOOR``. The cooldown policy (factory_track_record_cooldown)
  uses the same test. Each value reads ``FACTORY_HOT_*`` from the environment.
* With no tried model, the pick is the previous uniform hash pick exactly.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import random
import re
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WINDOW = "7d"
MIN_TRIALS = 3
EXPLORE_SHARE = 0.2
MAX_SHARE = 0.7
HOT_MAX_SHARE = 0.85
HOT_MIN_TRIALS = 10
HOT_LIFT = 1.5
HOT_FLOOR = 0.15
HOT_Z = 1.2816
PRIOR_TRIALS = 2.0
EVIDENCE_CAP = 50
PROVEN_BAD_TRIALS = 10
PROVEN_BAD_RATE = 0.05
DRAWS = 400
MAX_AGE_SECONDS = 2 * 24 * 3600
NON_MODEL_FAILURES = frozenset(
    {"control_plane_failure", "no_capacity", "environment_failure", "worker_environment_failure"}
)
DELIVERY_KINDS = frozenset({"implement"})
FILE_ENV = "FACTORY_TRACK_RECORD_FILE"
KIND_ENV = "FACTORY_ACTION_KIND"
PINNED_ENV = "REQUESTED_PROVIDER"
MERGE_CONFLICT = "factory:merge-conflict"
KIND_RE = re.compile(r"[a-z-]{0,40}")
SELECTION_LINE = re.compile(r"\[factory-model-selection\] (\{.*\})\s*$", re.M)


class NoUsableModelError(ValueError):
    """Every eligible model of the provider is proven bad for this action."""


@dataclass(frozen=True)
class Record:
    successes: int
    trials: int
    basis: str

    @property
    def proven_bad(self) -> bool:
        return self.trials >= PROVEN_BAD_TRIALS and self.successes < PROVEN_BAD_RATE * self.trials


def setting(name: str, default: float) -> float:
    """``FACTORY_<name>`` from the environment when it is a non-negative number."""
    try:
        value = float(os.environ.get(f"FACTORY_{name}", ""))
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def wilson_floor(record: Record) -> float:
    if record.trials <= 0:
        return 0.0
    z, n = HOT_Z, record.trials
    p = record.successes / n
    middle = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (middle - spread) / (1 + z * z / n))


def hot(record: Record | None, fleet_rate: float) -> bool:
    """Enough trials and a success rate clearly above the fleet's."""
    if record is None or record.trials < setting("HOT_MIN_TRIALS", HOT_MIN_TRIALS):
        return False
    bar = max(setting("HOT_FLOOR", HOT_FLOOR), setting("HOT_LIFT", HOT_LIFT) * fleet_rate)
    return wilson_floor(record) >= bar


def action_kind(assignment: Mapping[str, Any], labels: Collection[str] = ()) -> str:
    """The action a dispatched assignment asks for, named as the planner names it.

    The assignment stage never names a conflict, so the PR's labels decide that.
    """
    if assignment.get("kind") == "issue":
        return "implement"
    if assignment.get("kind") != "pr":
        return ""
    from factory_work_policy_backlog import completion_action_for

    stage = str(assignment.get("stage") or "")
    conflicted = MERGE_CONFLICT in labels or stage == MERGE_CONFLICT
    return completion_action_for(stage, conflicted) or "pr"


def _trial(window: Mapping[str, Any], kind: str) -> tuple[int, int]:
    def number(key: str) -> int:
        value = window.get(key)
        return value if isinstance(value, int) and value > 0 else 0

    failures = window.get("failures")
    ignored = (
        sum(v for k, v in failures.items() if k in NON_MODEL_FAILURES and isinstance(v, int))
        if isinstance(failures, Mapping)
        else 0
    )
    trials = number("completed") + max(number("failed") - ignored, 0)
    success = number("delivered") if kind in DELIVERY_KINDS else number("completed")
    return min(success, trials), trials


def _fresh(stats: Mapping[str, Any], now: datetime) -> bool:
    stamp = stats.get("counts_generated_at") or stats.get("generated_at")
    if not isinstance(stamp, str):
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return 0 <= (now - when).total_seconds() <= MAX_AGE_SECONDS


def parse(stats: object, kind: str, now: datetime) -> dict[tuple[str, str], Record]:
    """Per (source, model) record from a published stats snapshot; empty when stale."""
    if not isinstance(stats, Mapping) or not _fresh(stats, now):
        return {}
    rows = stats.get("models")
    found: dict[tuple[str, str], Record] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        source, model = row.get("source"), row.get("model")
        windows = row.get("windows")
        window = windows.get(WINDOW) if isinstance(windows, Mapping) else None
        if (
            not isinstance(source, str)
            or not isinstance(model, str)
            or not isinstance(window, Mapping)
        ):
            continue
        kinds = window.get("kinds")
        same = kinds.get(kind) if kind and isinstance(kinds, Mapping) else None
        if isinstance(same, Mapping):
            successes, trials = _trial(same, kind)
            if trials >= MIN_TRIALS:
                found[(source, model)] = Record(successes, trials, kind)
                continue
        successes, trials = _trial(window, kind)
        if trials:
            found[(source, model)] = Record(successes, trials, "all")
    return found


@functools.lru_cache(maxsize=4)
def _load(path: str, mtime: int, kind: str) -> dict[tuple[str, str], Record]:
    try:
        stats = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return parse(stats, kind, datetime.now(UTC))


def records(kind: str | None = None) -> dict[tuple[str, str], Record]:
    """Records for ``kind``; by default the action kind this job runs."""
    path = os.environ.get(FILE_ENV, "")
    if not path:
        return {}
    try:
        mtime = Path(path).stat().st_mtime_ns
    except OSError:
        return {}
    return _load(path, mtime, os.environ.get(KIND_ENV, "") if kind is None else kind)


def has_history(source: str, models: Sequence[str]) -> bool:
    found = records()
    return any(
        (record := found.get((source, model))) is not None and record.trials >= MIN_TRIALS
        for model in models
    )


def proven_bad(source: str, models: Sequence[str]) -> set[str]:
    found = records()
    return {
        model for model in models if (r := found.get((source, model))) is not None and r.proven_bad
    }


def fleet_prior(found: Mapping[tuple[str, str], Record]) -> float:
    tried = [r for r in found.values() if r.trials >= MIN_TRIALS]
    return (sum(r.successes for r in tried) + 1) / (sum(r.trials for r in tried) + 2)


def proven(source: str, models: Sequence[str]) -> set[str]:
    """Models with enough trials and at least one success."""
    found = records()
    return {
        model
        for model in models
        if (record := found.get((source, model))) is not None
        and record.trials >= MIN_TRIALS
        and record.successes > 0
    }


def _unit(raw: bytes) -> float:
    return int.from_bytes(raw, "big") / 2**64


def _score(record: Record, prior: float) -> tuple[float, float]:
    scale = min(1.0, EVIDENCE_CAP / record.trials)
    alpha = prior * PRIOR_TRIALS + record.successes * scale
    beta = (1 - prior) * PRIOR_TRIALS + (record.trials - record.successes) * scale
    return alpha, beta


def shares(source: str, tried: dict[str, Record], prior: float | None = None) -> dict[str, float]:
    """Thompson win probabilities, capped per model; the same for every worker."""
    names = sorted(tried)
    if prior is None:
        prior = fleet_prior({(source, name): record for name, record in tried.items()})
    params = [_score(tried[name], prior) for name in names]
    seed = hashlib.sha256(f"{source}:{','.join(names)}".encode()).digest()
    rng = random.Random(int.from_bytes(seed[:8], "big"))
    wins = dict.fromkeys(names, 0)
    for _ in range(DRAWS):
        draws = [rng.betavariate(a, b) for a, b in params]
        wins[names[draws.index(max(draws))]] += 1
    result = {name: wins[name] / DRAWS for name in names}
    if len(names) < 2:
        return result
    top = setting("HOT_MAX_SHARE", HOT_MAX_SHARE)
    cap = {n: max(MAX_SHARE, min(top, 1.0)) if hot(tried[n], prior) else MAX_SHARE for n in names}
    capped: set[str] = set()
    while True:
        over = [n for n in names if n not in capped and result[n] > cap[n] + 1e-12]
        if not over:
            return result
        for name in over:
            capped.add(name)
            excess = result[name] - cap[name]
            result[name] = cap[name]
            rest = [n for n in names if n not in capped]
            if not rest:
                return result
            total = sum(result[n] for n in rest)
            for other in rest:
                result[other] += excess * (result[other] / total if total else 1 / len(rest))


def pick(source: str, models: Sequence[str], seed: str) -> tuple[str, str]:
    """One model from ``models`` and why; ``seed`` is the selector's own legacy hash input."""
    if not models:
        raise ValueError("no eligible models")
    digest = hashlib.sha256(seed.encode()).digest()
    found = records()
    bad = [m for m in models if (r := found.get((source, m))) is not None and r.proven_bad]
    if bad and os.environ.get(PINNED_ENV, "auto") == source:
        bad = []
    if bad:
        models = [m for m in models if m not in bad]
        if not models:
            raise NoUsableModelError(
                f"every eligible {source} model is proven bad: {PROVEN_BAD_TRIALS}+ trials and "
                f"under {PROVEN_BAD_RATE:.0%} success in {WINDOW}"
            )
    skipped = f"skipped {len(bad)} proven-bad; " if bad else ""
    legacy = models[int.from_bytes(digest[:8], "big") % len(models)]
    sampled = {
        m: r for m in models if (r := found.get((source, m))) is not None and r.trials >= MIN_TRIALS
    }
    tried = {m: r for m, r in sampled.items() if r.successes > 0}
    if not tried:
        need = "a success" if sampled else f"{MIN_TRIALS}+ trials"
        return legacy, f"{skipped}uniform: no eligible {source} model has {need} in {WINDOW}"
    untried = [m for m in models if m not in tried]
    if untried and _unit(digest[8:16]) < EXPLORE_SHARE:
        model = untried[int.from_bytes(digest[24:32], "big") % len(untried)]
        record = found.get((source, model))
        history = (
            f"{record.trials} trials and no success"
            if record and record.trials >= MIN_TRIALS
            else f"{record.trials if record else 0} trials (under {MIN_TRIALS})"
        )
        return model, (
            f"{skipped}explore: {history} in {WINDOW}; "
            f"{EXPLORE_SHARE:.0%} of picks try models with little history"
        )
    prior = fleet_prior(found)
    weights = shares(source, tried, prior)
    point = _unit(digest[16:24])
    chosen = sorted(tried)[-1]
    for name in sorted(tried):
        point -= weights[name]
        if point < 0:
            chosen = name
            break
    record = tried[chosen]
    return chosen, (
        f"{skipped}track record: {record.successes} of {record.trials} {record.basis} runs "
        f"succeeded in {WINDOW}; score {record.successes / record.trials:.2f}; "
        f"pick share {weights[chosen]:.2f}{'; hot' if hot(record, prior) else ''}"
    )


def logged_selection(log: str) -> dict[str, str]:
    """Kind and reason from the first selection marker, which precedes any model output."""
    match = SELECTION_LINE.search(log)
    try:
        found = json.loads(match.group(1)) if match else None
    except ValueError:
        found = None
    if not isinstance(found, dict):
        return {"kind": "", "reason": ""}
    kind = str(found.get("kind") or "")
    picked = f"{found.get('source') or '?'} / {found.get('model') or '?'}"
    return {
        "kind": kind if KIND_RE.fullmatch(kind) else "",
        "reason": f"{picked}: {found.get('reason') or ''}"[:300],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("kind", "marker"))
    parser.add_argument("--log", type=Path)
    parser.add_argument("--labels-json", default="[]")
    args = parser.parse_args(argv)
    if args.command == "marker":
        if args.log is None:
            parser.error("marker needs --log")
        log = args.log.read_text(encoding="utf-8", errors="replace")
        print(json.dumps(logged_selection(log)))
        return 0
    try:
        assignment = json.loads(os.environ.get("FACTORY_ASSIGNMENT") or "{}")
        labels = json.loads(args.labels_json)
    except ValueError:
        assignment, labels = {}, []
    if not isinstance(assignment, dict):
        assignment = {}
    names = [x for x in labels if isinstance(x, str)] if isinstance(labels, list) else []
    print(action_kind(assignment, names))
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    sys.exit(main())
