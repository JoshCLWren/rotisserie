"""Pick the provider and model with the best delivered track record.

Providers and models turned off with the fleet switch are never picked here,
and neither is a model the worker's published exclusions list. When those
exclusions cannot be read, the provider is pinned and the worker picks the
exact model. Naming both a provider and a model is an explicit choice and is
used as given.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import (
    catalog,
    fleet,
    model_exclusions,
    providers,
    reliability,
)
from rotisserie.factory_console.service.steps import Step, quiet


def choose_models(
    github: GitHub,
    variables: dict[str, str],
    now: datetime,
    provider: str | None,
    model: str | None,
    step: Step = quiet,
) -> tuple[str, list[tuple[str, str, str]], tuple[str, ...], tuple[str, ...]]:
    """Ranked (provider key, model, why) choices, best first, and why others were skipped."""
    if provider and model:
        found = runtime.resolve_provider(provider)
        runtime.validate_model(found, model)
        return "you chose it", [(found.key, model, "you chose it")], (), ()
    step("Reading the published model track records", phase="models")
    stats = reliability.load_stats(github)
    if stats is None:
        return (
            "no track record is published yet",
            [],
            ("no reliability snapshot to pick a model from; name PROVIDER MODEL",),
            (),
        )
    step("Checking which providers are set up", phase="models")
    setup = providers.provider_setup(github, variables)
    named = runtime.resolve_provider(provider).key if provider else None
    ready = {
        item.provider.key
        for item in setup
        if item.ready or (item.provider.key == named and item.known and not item.missing)
    }
    if named:
        ready &= {named}
    switched = fleet.state(variables)
    excluded = model_exclusions.read(stats, now)
    if excluded is None:
        step(
            "The worker's current exclusions are not published; the worker picks each model",
            phase="models",
        )
    step("Checking which models each provider still offers", phase="models")
    skipped: list[tuple[str, str]] = []
    choices = reliability.pick_models(
        stats,
        ready=ready,
        now=now,
        live=catalog.live_models,
        disabled=lambda found, name: switched.model_off(found.source, name) is not None,
        blocked=excluded.reason if excluded else None,
        skipped=skipped,
    )
    summary = skip_summary(skipped)
    for line in summary:
        step(f"Skipped {line}", phase="models")
    if not choices:
        scope = (
            f"every {runtime.resolve_provider(provider).name} model" if provider else "every model"
        )
        why = (
            f"{scope} with a track record is skipped right now: {'; '.join(summary)}"
            if summary
            else "no set-up, unpaused model that the provider still offers has a track record"
        )
        return (
            "no set-up model has delivered recently",
            [],
            (f"{why}; name PROVIDER MODEL",),
            tuple(summary),
        )
    ranked: list[tuple[str, str, str]] = []
    pinned: set[str] = set()
    for item in choices:
        if excluded is not None and not excluded.unverifiable(item.provider):
            ranked.append((item.provider.key, item.model, item.why))
        elif item.provider.key not in pinned:
            pinned.add(item.provider.key)
            cause = (
                "its current exclusions are not published"
                if excluded is None
                else "its paid models are cooling and the snapshot does not say which are free"
            )
            ranked.append(
                (item.provider.key, "auto", f"{item.why}; the worker picks the model: {cause}")
            )
    return f"best track record: {ranked[0][2]}", ranked, (), tuple(summary)


def skip_summary(skipped: list[tuple[str, str]], limit: int = 4) -> list[str]:
    """One line per reason, naming at most ``limit`` models each."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for model, why in skipped:
        if model not in grouped[why]:
            grouped[why].append(model)
    lines = []
    for why, models in grouped.items():
        names = ", ".join(models[:limit])
        more = f" and {len(models) - limit} more" if len(models) > limit else ""
        lines.append(f"{names}{more}: {why}")
    return lines
