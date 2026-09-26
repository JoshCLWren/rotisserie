#!/usr/bin/env python3
"""Select a provider candidate using trusted durable runtime evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from factory_completion_controller import (
    FAILURE_COOLDOWN_SECONDS,
    RATE_LIMIT_COOLDOWN_SECONDS,
    parse_time,
)
from factory_work_policy import comment_is_trusted


PROVIDER_RE = re.compile(r"(?m)^Source:\s*(?P<provider>\S+)\s*$")
MODEL_RE = re.compile(r"(?m)^Model:\s*(?P<model>\S+)\s*$")
RETIREMENT_MARKER = "<!-- factory-model-retired-410:v1 -->"
OUTCOME_RE = re.compile(
    r"(?m)^Attempt outcome:\s*(?P<outcome>[a-z][a-z0-9_]+)\s*$"
)
UPDATED_RE = re.compile(r"(?m)^Updated:\s*(?P<updated>\S+)\s*$")
HEALTHY_OUTCOMES = {
    "success",
    "no_work",
    "work_failure",
    # Historical aliases retained while durable records age out.
    "no_change",
    "policy_blocked",
}
IGNORED_MODEL_OUTCOMES = {
    "control_plane_failure",
    "environment_failure",
    # Historical alias.
    "worker_environment_failure",
}
PROVIDER_FAILURE_OUTCOMES = {
    "provider_failure",
    "provider_throttle",
    # Historical aliases.
    "provider_unavailable",
    "provider_throttled",
}
PERMANENT_MODEL_OUTCOMES = {
    "model_unavailable",
    "model_policy_violation",
    "model_retired_410",
}
TRANSIENT_MODEL_OUTCOMES = {
    "unknown_failure",
    # Historical alias. New classifiers emit provider_failure instead.
    "model_interruption",
}
# OmniRoute is the GitHub execution gateway, but it exposes independent model
# routes. Keep throttle/failure evidence scoped to the affected route so one
# busy upstream model does not suppress the rest of the fleet.
MODEL_SCOPED_PROVIDERS = {"omniroute-free"}
# These are virtual intent routes, not backing models. Their executable health
# is established by the bounded OpenCode smoke at the execution boundary. They
# must never be suppressed because a concrete backing model lacks historical
# health evidence in ComicPile.
NATIVE_OMNIROUTE_ROUTES = {"auto/coding:free", "auto/reasoning:free"}


@dataclass(frozen=True)
class AttemptEvidence:
    """Latest normalized outcome for one provider/model attempt."""

    provider: str
    model: str
    outcome: str
    updated: int


@dataclass(frozen=True)
class RankedCandidate:
    """One discovered candidate annotated with runtime health."""

    provider: str
    model: str
    runtime_model: str
    discovered_by: str
    health_state: str


@dataclass(frozen=True)
class Selection:
    """Health-ranked selection result for one dispatched slot."""

    selected: RankedCandidate | None
    candidates: tuple[RankedCandidate, ...]
    failure_outcome: str
    detail: str


def _ranking_key(model: str) -> str:
    """Normalize a catalog model for the documented Arena provider suffixes."""
    value = model.strip().lower()
    if value.endswith(":free"):
        value = value[:-5]
    if value.startswith("openrouter/"):
        value = value[len("openrouter/"):]
    return value


def load_code_rankings(path: Path | None) -> dict[str, float]:
    """Load exact code leaderboard scores, failing closed on malformed data."""
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    fetched_at = payload.get("fetched_at")
    if not isinstance(fetched_at, int) or int(time.time()) - fetched_at > 7 * 24 * 60 * 60:
        return {}
    models = payload.get("models")
    if not isinstance(models, list):
        return {}
    rankings: dict[str, float] = {}
    for item in models:
        if not isinstance(item, Mapping):
            continue
        model = item.get("model")
        score = item.get("score")
        if isinstance(model, str) and isinstance(score, (int, float)):
            rankings[_ranking_key(model)] = float(score)
    return rankings


def flatten_comments(payload: object) -> list[Mapping[str, Any]]:
    """Flatten one page or a slurped list of GitHub comment pages."""
    if not isinstance(payload, list):
        return []
    flattened: list[Mapping[str, Any]] = []
    for item in payload:
        if isinstance(item, Mapping):
            flattened.append(item)
        elif isinstance(item, list):
            flattened.extend(value for value in item if isinstance(value, Mapping))
    return flattened


def latest_attempt_evidence(
    comments: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], AttemptEvidence]:
    """Return the latest trusted normalized attempt per provider/model."""
    latest: dict[tuple[str, str], AttemptEvidence] = {}
    for comment in comments:
        if not comment_is_trusted(comment):
            continue
        body = str(comment.get("body") or "")
        provider_match = PROVIDER_RE.search(body)
        model_match = MODEL_RE.search(body)
        if RETIREMENT_MARKER in body:
            updated_match = UPDATED_RE.search(body)
            if provider_match is None or model_match is None or updated_match is None:
                continue
            updated = parse_time(updated_match.group("updated"))
            if updated is None:
                continue
            evidence = AttemptEvidence(
                provider=provider_match.group("provider"),
                model=model_match.group("model"),
                outcome="model_retired_410",
                updated=updated,
            )
            key = (evidence.provider, evidence.model)
            previous = latest.get(key)
            if previous is None or previous.outcome != "model_retired_410":
                latest[key] = evidence
            continue
        outcome_match = OUTCOME_RE.search(body)
        updated_match = UPDATED_RE.search(body)
        if (
            provider_match is None
            or model_match is None
            or outcome_match is None
            or updated_match is None
        ):
            continue
        updated = parse_time(updated_match.group("updated"))
        if updated is None:
            continue
        outcome = outcome_match.group("outcome")
        if outcome in IGNORED_MODEL_OUTCOMES:
            continue
        evidence = AttemptEvidence(
            provider=provider_match.group("provider"),
            model=model_match.group("model"),
            outcome=outcome,
            updated=updated,
        )
        key = (evidence.provider, evidence.model)
        previous = latest.get(key)
        if previous is not None and previous.outcome == "model_retired_410":
            continue
        if previous is None or evidence.updated > previous.updated:
            latest[key] = evidence
    return latest


def _provider_state(
    provider: str,
    evidence: Iterable[AttemptEvidence],
    *,
    now_epoch: int,
) -> str:
    """Return provider health from the newest provider-relevant attempt."""
    relevant = [
        item
        for item in evidence
        if item.provider == provider and item.outcome != "unknown_failure"
    ]
    if not relevant:
        return "unknown"
    newest = max(relevant, key=lambda item: item.updated)
    if newest.outcome not in PROVIDER_FAILURE_OUTCOMES:
        return "healthy"
    cooldown = (
        RATE_LIMIT_COOLDOWN_SECONDS
        if newest.outcome in {"provider_throttle", "provider_unavailable", "provider_throttled"}
        else FAILURE_COOLDOWN_SECONDS
    )
    if now_epoch < newest.updated + cooldown:
        return "cooling"
    return "degraded"


def _model_state(
    provider: str,
    model: str,
    evidence: Mapping[tuple[str, str], AttemptEvidence],
    *,
    now_epoch: int,
) -> str:
    """Return model health without attributing control-plane faults to it."""
    item = evidence.get((provider, model))
    if item is None:
        return "unknown"
    if item.outcome in HEALTHY_OUTCOMES:
        return "healthy"
    if item.outcome in PERMANENT_MODEL_OUTCOMES:
        return "unavailable"
    if item.outcome in IGNORED_MODEL_OUTCOMES:
        return "unknown"
    if item.outcome in TRANSIENT_MODEL_OUTCOMES:
        if now_epoch < item.updated + FAILURE_COOLDOWN_SECONDS:
            return "cooling"
        return "degraded"
    if item.outcome in PROVIDER_FAILURE_OUTCOMES:
        if provider in MODEL_SCOPED_PROVIDERS:
            cooldown = (
                RATE_LIMIT_COOLDOWN_SECONDS
                if item.outcome in {"provider_throttle", "provider_unavailable", "provider_throttled"}
                else FAILURE_COOLDOWN_SECONDS
            )
            if now_epoch < item.updated + cooldown:
                return "cooling"
            return "degraded"
        return "unknown"
    return "unknown"


def rank_candidates(
    candidates: Sequence[Mapping[str, Any]],
    comments: Iterable[Mapping[str, Any]],
    *,
    now_epoch: int,
) -> tuple[RankedCandidate, ...]:
    """Annotate discovered candidates with combined provider/model health."""
    evidence = latest_attempt_evidence(comments)
    attempts = tuple(evidence.values())
    ranked: list[RankedCandidate] = []
    for candidate in candidates:
        provider = str(candidate.get("provider") or "")
        model = str(candidate.get("model") or "")
        runtime_model = str(candidate.get("runtime_model") or "")
        discovered_by = str(candidate.get("discovered_by") or "")
        if not all((provider, model, runtime_model, discovered_by)):
            continue
        native = _native_omniroute_candidate(candidate)
        if native is not None:
            ranked.append(native)
            continue

        provider_state = (
            "unknown"
            if provider in MODEL_SCOPED_PROVIDERS
            else _provider_state(provider, attempts, now_epoch=now_epoch)
        )
        model_state = _model_state(
            provider,
            model,
            evidence,
            now_epoch=now_epoch,
        )
        if model_state == "unavailable":
            state = "unavailable"
        elif provider_state == "cooling" or model_state == "cooling":
            state = "cooling"
        elif model_state == "healthy" and provider_state in {
            "healthy",
            "degraded",
            "unknown",
        }:
            state = "healthy" if provider_state != "degraded" else "degraded"
        elif model_state == "degraded" or provider_state == "degraded":
            state = "degraded"
        else:
            state = "unknown"
        ranked.append(
            RankedCandidate(
                provider=provider,
                model=model,
                runtime_model=runtime_model,
                discovered_by=discovered_by,
                health_state=state,
            )
        )
    return tuple(ranked)


def _native_omniroute_candidate(
    candidate: Mapping[str, Any],
) -> RankedCandidate | None:
    """Return a trusted native intent route without inspecting backing models."""
    provider = str(candidate.get("provider") or "")
    model = str(candidate.get("model") or "")
    runtime_model = str(candidate.get("runtime_model") or "")
    discovered_by = str(candidate.get("discovered_by") or "")
    if provider != "omniroute-free" or model not in NATIVE_OMNIROUTE_ROUTES:
        return None
    if runtime_model != f"omniroute/{model}" or not discovered_by:
        return None
    return RankedCandidate(
        provider=provider,
        model=model,
        runtime_model=runtime_model,
        discovered_by=discovered_by,
        health_state="native_route",
    )


def select_candidate(
    candidates: Sequence[Mapping[str, Any]],
    comments: Iterable[Mapping[str, Any]],
    *,
    worker: int,
    now_epoch: int,
    preferred_provider: str | None = None,
    rankings: Mapping[str, float] | None = None,
    require_rankings: bool = False,
) -> Selection:
    """Prefer executable candidates while preserving native OmniRoute intents."""
    # Native OmniRoute intents are the execution boundary. ComicPile must not
    # inspect or veto the concrete free model/provider selected behind them.
    # Their executable capacity is proven immediately afterward by the bounded
    # OpenCode smoke, which provides better evidence than stale backing-model
    # attempt history.
    native = tuple(
        value
        for candidate in candidates
        if (value := _native_omniroute_candidate(candidate)) is not None
    )
    if native and preferred_provider in {None, "omniroute-free"}:
        # Carry the already-resolved native intent. ComicPile must not pick
        # among intent lanes or backing models by worker identity.
        selected = native[0]
        ranking_note = (
            "; Arena ranking is preference-only and is not a capacity gate"
            if require_rankings
            else ""
        )
        return Selection(
            selected=selected,
            candidates=native,
            failure_outcome="",
            detail=(
                "selected native OmniRoute intent route; smoke owns executable health"
                + ranking_note
            ),
        )

    comment_list = tuple(comments)
    ranked = rank_candidates(candidates, comment_list, now_epoch=now_epoch)
    # Catalog presence is not execution evidence for concrete model selectors.
    # Unknown backing-model routes still fail closed.
    priority = {"healthy": 0, "degraded": 1}
    usable = [candidate for candidate in ranked if candidate.health_state in priority]
    preferred = [
        candidate
        for candidate in usable
        if preferred_provider and candidate.provider == preferred_provider
    ]
    if preferred:
        usable = preferred
    if usable:
        best_priority = min(priority[candidate.health_state] for candidate in usable)
        best = sorted(
            (
                candidate
                for candidate in usable
                if priority[candidate.health_state] == best_priority
            ),
            key=lambda candidate: (
                -(rankings or {}).get(_ranking_key(candidate.model), float("-inf")),
                candidate.model,
                candidate.runtime_model,
            ),
        )
        selected = best[(worker - 1) % len(best)]
        return Selection(
            selected=selected,
            candidates=ranked,
            failure_outcome="",
            detail=(
                f"selected {selected.health_state} candidate"
                f"{f' from preferred provider {preferred_provider}' if preferred else ''}"
                " from "
                f"{len(best)} best-tier option(s)"
                + (
                    "; Arena ranking is preference-only and is not a capacity gate"
                    if require_rankings
                    else ""
                )
            ),
        )

    states = {candidate.health_state for candidate in ranked}
    evidence = latest_attempt_evidence(comment_list)
    attempts = tuple(evidence.values())
    providers = {candidate.provider for candidate in ranked}
    provider_cooling = any(
        _provider_state(provider, attempts, now_epoch=now_epoch) == "cooling"
        for provider in providers
    )
    cooling_outcomes = {
        item.outcome
        for candidate in ranked
        if candidate.health_state == "cooling"
        for item in [evidence.get((candidate.provider, candidate.model))]
        if item is not None
    }
    if provider_cooling:
        if "provider_throttle" in cooling_outcomes or "provider_throttled" in cooling_outcomes:
            failure = "provider_throttle"
            detail = "provider throttle evidence is cooling all discovered candidates"
        else:
            failure = "provider_failure"
            detail = "provider failure evidence is cooling all discovered candidates"
    elif "unknown_failure" in cooling_outcomes:
        failure = "unknown_failure"
        detail = "unknown candidate failures are still cooling"
    elif "model_interruption" in cooling_outcomes:
        failure = "provider_failure"
        detail = "legacy transient interruption evidence is still cooling"
    elif "cooling" in states:
        failure = "unknown_failure"
        detail = "all discovered candidates are cooling without safe attribution"
    elif states == {"unavailable"}:
        model_policy_only = bool(ranked) and all(
            (evidence.get((candidate.provider, candidate.model)) or AttemptEvidence("", "", "", 0)).outcome
            == "model_policy_violation"
            for candidate in ranked
        )
        failure = "model_policy_violation" if model_policy_only else "model_unavailable"
        detail = "all discovered candidates have permanent model-scoped evidence"
    else:
        failure = "unknown_failure"
        detail = "no discovered candidate has executable health"
    return Selection(
        selected=None,
        candidates=ranked,
        failure_outcome=failure,
        detail=detail,
    )


def main() -> int:
    """Select one candidate from discovery JSON and durable issue comments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--comments", type=Path, required=True)
    parser.add_argument("--worker", type=int, required=True)
    parser.add_argument("--preferred-provider", default=None)
    parser.add_argument("--rankings", type=Path, default=None)
    parser.add_argument("--require-rankings", action="store_true")
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()

    discovery = json.loads(args.discovery.read_text(encoding="utf-8"))
    comments_payload = json.loads(args.comments.read_text(encoding="utf-8"))
    candidates = discovery.get("candidates") if isinstance(discovery, Mapping) else None
    if not isinstance(candidates, list):
        candidates = []
    selection = select_candidate(
        candidates,
        flatten_comments(comments_payload),
        worker=args.worker,
        now_epoch=args.now if args.now is not None else int(time.time()),
        preferred_provider=args.preferred_provider,
        rankings=load_code_rankings(args.rankings),
        require_rankings=args.require_rankings,
    )
    print(json.dumps(asdict(selection), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())