"""Pick the provider and model with the best delivered track record.

Providers and models turned off with the fleet switch are never picked here.
Naming both a provider and a model is an explicit choice and is used as given.
"""

from __future__ import annotations

from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import catalog, fleet, providers, reliability
from rotisserie.factory_console.service.steps import Step, quiet


def choose_models(
    github: GitHub,
    variables: dict[str, str],
    now: datetime,
    provider: str | None,
    model: str | None,
    step: Step = quiet,
) -> tuple[str, list[tuple[str, str, str]], tuple[str, ...]]:
    """Ranked (provider key, model, why) choices, best first."""
    if provider and model:
        found = runtime.resolve_provider(provider)
        runtime.validate_model(found, model)
        return "you chose it", [(found.key, model, "you chose it")], ()
    step("Reading the published model track records", phase="models")
    stats = reliability.load_stats(github)
    if stats is None:
        return (
            "no track record is published yet",
            [],
            ("no reliability snapshot to pick a model from; name PROVIDER MODEL",),
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
    step("Checking which models each provider still offers", phase="models")
    choices = reliability.pick_models(
        stats,
        ready=ready,
        now=now,
        live=catalog.live_models,
        disabled=lambda found, name: switched.model_off(found.source, name) is not None,
    )
    if not choices:
        return (
            "no set-up model has delivered recently",
            [],
            (
                "no set-up, unpaused model that the provider still offers has a track record; "
                "name PROVIDER MODEL",
            ),
        )
    ranked = [(item.provider.key, item.model, item.why) for item in choices]
    return f"best track record: {choices[0].why}", ranked, ()
