"""Provider and model track record, from the factory's published reliability snapshot.

The dashboard job publishes ``factory-stats.json`` (counts of trusted worker
outcomes) to the ``factory-status-live`` branch. Nothing here estimates: a
model with no runs has no record, and a model that is paused is reported as
paused only while its cooldown lasts.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    JSON,
    GitHub,
    GitHubNotFoundError,
    Obj,
    integer,
    items,
    obj,
    string,
    timestamp,
)

STATS_BRANCH = "factory-status-live"
PAUSED = ("cooling", "unavailable")


def load_stats(github: GitHub) -> Obj | None:
    """The published snapshot, or None when none has been published."""
    try:
        content = obj(github.repo_api(f"contents/factory-stats.json?ref={STATS_BRANCH}"))
    except GitHubNotFoundError:
        return None
    raw = string(content, "content")
    if not raw:
        return None
    try:
        parsed: JSON = json.loads(base64.b64decode(raw))
    except ValueError:
        return None
    return obj(parsed)


def window(entry: Obj, name: str) -> Obj:
    return obj(obj(entry.get("windows")).get(name))


def count(counts: Obj, key: str) -> int:
    return integer(counts, key) or 0


def health_status(health: Obj, now: datetime) -> str:
    """``paused`` only while a cooldown is still in the future; else the recorded status."""
    status = string(health, "status")
    if status in PAUSED:
        until = timestamp(health, "cooldown_until")
        if until is not None and until <= now:
            return "healthy"
        return "paused"
    return status or "unknown"


@dataclass(frozen=True)
class ModelRecord:
    """One model's finished worker runs in a window, and whether it may run now."""

    source: str
    model: str
    runs: int
    delivered: int
    failed: int
    status: str  # healthy | paused | retired | unknown
    paused_until: datetime | None

    @property
    def delivery_rate(self) -> float:
        return self.delivered / self.runs if self.runs else 0.0


def model_records(stats: Obj, *, name: str, now: datetime) -> list[ModelRecord]:
    records = []
    for entry in items(stats, "models"):
        counts = window(entry, name)
        health = obj(entry.get("health"))
        status = health_status(health, now)
        records.append(
            ModelRecord(
                string(entry, "source"),
                string(entry, "model"),
                count(counts, "runs"),
                count(counts, "delivered"),
                count(counts, "failed"),
                status,
                timestamp(health, "cooldown_until") if status == "paused" else None,
            )
        )
    return records


@dataclass(frozen=True)
class Choice:
    """A provider and exact model picked for its delivered track record."""

    provider: runtime.Provider
    model: str
    record: ModelRecord
    why: str
    checked: bool = True  # the provider's live list includes it


def dispatch_model_id(provider: runtime.Provider, model: str) -> str | None:
    """The exact model ID the dispatcher accepts for a recorded model, if any."""
    for candidate in (model, provider.model_prefix + model):
        if not candidate or (
            provider.model_prefix and not candidate.startswith(provider.model_prefix)
        ):
            continue
        try:
            runtime.validate_model(provider, candidate)
        except ValueError:
            continue
        return candidate
    return None


def pick_models(
    stats: Obj,
    *,
    ready: set[str],
    now: datetime,
    name: str = "7d",
    minimum_runs: int = 3,
    live: Callable[[runtime.Provider], frozenset[str] | None] | None = None,
    disabled: Callable[[runtime.Provider, str], bool] | None = None,
    blocked: Callable[[runtime.Provider, str, str], str | None] | None = None,
    skipped: list[tuple[str, str]] | None = None,
) -> list[Choice]:
    """Models that can run now, best delivered track record first.

    ``ready`` holds provider keys whose credentials are set up. Paused and
    retired models are left out; so are models with fewer than
    ``minimum_runs`` runs or nothing delivered, because one lucky run is not
    a track record. ``live`` lists the models a provider offers right now; a
    model missing from a readable list is left out (the worker would refuse
    it), and one whose provider has no readable list is kept but ranked after
    every checked model. ``disabled`` names models turned off with the fleet switch.
    ``blocked`` gives the worker's reason to refuse a (provider, recorded, exact)
    model now. Each candidate with a record that is left out lands in ``skipped``
    as (exact model, why).
    """
    providers = {provider.source: provider for provider in runtime.providers()}
    choices = []
    offered: dict[str, frozenset[str] | None] = {}
    for record in model_records(stats, name=name, now=now):
        provider = providers.get(record.source)
        if provider is None or provider.key not in ready:
            continue
        if record.status in ("paused", "retired") or record.runs < minimum_runs:
            continue
        if record.delivered == 0:
            continue
        model = dispatch_model_id(provider, record.model)
        if model is None:
            continue
        if disabled is not None and disabled(provider, model):
            _skip(skipped, model, "turned off with the fleet switch")
            continue
        refused = blocked(provider, record.model, model) if blocked else None
        if refused:
            _skip(skipped, model, refused)
            continue
        if provider.key not in offered:
            offered[provider.key] = live(provider) if live else None
        listed = offered[provider.key]
        if listed is not None and model not in listed:
            _skip(skipped, model, "the provider no longer offers it")
            continue
        why = (
            f"{record.delivered} of {record.runs} runs delivered a pull request "
            f"in the last {'7 days' if name == '7d' else '24 hours'}"
        )
        if listed is None:
            why += " (could not check that the provider still offers it)"
        choices.append(Choice(provider, model, record, why, checked=listed is not None))
    return sorted(
        choices,
        key=lambda item: (
            not item.checked,
            -item.record.delivery_rate,
            -item.record.delivered,
            item.model,
        ),
    )


def _skip(skipped: list[tuple[str, str]] | None, model: str, why: str) -> None:
    if skipped is not None:
        skipped.append((model, why))
