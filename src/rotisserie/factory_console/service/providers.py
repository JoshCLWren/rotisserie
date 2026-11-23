"""Which factory providers are set up in the repository (credentials and switches)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError


def repository_variables(github: GitHub) -> dict[str, str]:
    """Repository variables, with the workflow-delivered settings snapshot winning.

    A workflow's ``FACTORY_REPO_VARS`` carries the registered settings even when
    its token cannot read the Variables API. Other variables (for example
    ``FACTORY_MAIN_CI_WORKFLOWS``) still come from the API whenever it answers.
    """
    delivered: dict[str, str] = runtime.module("factory_settings").repository_variables()
    try:
        raw = github.gh("variable", "list", "--repo", github.repo, "--json", "name,value")
        rows = json.loads(raw)
    except (GitHubError, ValueError):
        return delivered
    listed = {
        str(row["name"]): str(row["value"])
        for row in rows
        if isinstance(row, dict) and "name" in row
    }
    return {**listed, **delivered}


def secret_names(github: GitHub) -> set[str] | None:
    """Repository secret names, or None when the login cannot list them."""
    try:
        raw = github.gh("secret", "list", "--repo", github.repo, "--json", "name")
        rows = json.loads(raw)
    except (GitHubError, ValueError):
        return None
    return {str(row["name"]) for row in rows if isinstance(row, dict) and "name" in row}


@dataclass(frozen=True)
class Setup:
    provider: runtime.Provider
    known: bool  # False when secrets could not be listed
    missing: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.known and not self.missing


def provider_setup(github: GitHub, variables: dict[str, str] | None = None) -> list[Setup]:
    secrets = secret_names(github)
    values = repository_variables(github) if variables is None else variables
    result = []
    for provider in runtime.providers():
        missing = [
            name
            for name in provider.secrets
            if secrets is not None and name not in secrets and name not in values
        ]
        if provider.needs_variable and values.get(provider.needs_variable, "off").lower() not in (
            "on",
            "true",
            "1",
            "yes",
        ):
            missing.append(f"{provider.needs_variable}=on")
        result.append(Setup(provider, secrets is not None, tuple(missing)))
    return result
