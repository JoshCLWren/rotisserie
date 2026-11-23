"""Registered workflow settings remain authoritative without GitHub Variables API."""

from __future__ import annotations

from typing import cast

import pytest

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError
from rotisserie.factory_console.service import providers


def test_workflow_settings_precede_inaccessible_variables_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACTORY_REPO_VARS", '{"FACTORY_PR_WIP_LIMIT": "1"}')

    class ReadOnlyGitHub:
        repo = "acme/widgets"

        def gh(self, *args: str, **kwargs: object) -> str:
            raise GitHubError("Resource not accessible by integration")

    values = providers.repository_variables(cast(GitHub, ReadOnlyGitHub()))
    assert values == {"FACTORY_PR_WIP_LIMIT": "1"}
    assert runtime.setting_integer("FACTORY_PR_WIP_LIMIT", values) == 1


def test_read_only_gh_variables_api_is_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FACTORY_REPO_VARS", raising=False)

    class ReadOnlyGitHub:
        repo = "acme/widgets"

        def gh(self, *args: str, **kwargs: object) -> str:
            assert args[:2] == ("variable", "list")
            return '[{"name": "FACTORY_PR_WIP_LIMIT", "value": "2"}]'

    values = providers.repository_variables(cast(GitHub, ReadOnlyGitHub()))
    assert values == {"FACTORY_PR_WIP_LIMIT": "2"}
    assert runtime.setting_integer("FACTORY_PR_WIP_LIMIT", values) == 2


def test_no_workflow_settings_and_unavailable_api_fail_to_default_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FACTORY_REPO_VARS", raising=False)

    class ForbiddenGitHub:
        repo = "acme/widgets"

        def gh(self, *args: str, **kwargs: object) -> str:
            raise GitHubError("cannot read variables")

    assert providers.repository_variables(cast(GitHub, ForbiddenGitHub())) == {}


def test_delivered_settings_win_without_hiding_other_repository_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACTORY_REPO_VARS", '{"FACTORY_PR_WIP_LIMIT": "1"}')

    class ListingGitHub:
        repo = "acme/widgets"

        def gh(self, *args: str, **kwargs: object) -> str:
            assert args[:2] == ("variable", "list")
            return (
                '[{"name": "FACTORY_PR_WIP_LIMIT", "value": "9"},'
                ' {"name": "FACTORY_MAIN_CI_WORKFLOWS", "value": "rust.yml"}]'
            )

    values = providers.repository_variables(cast(GitHub, ListingGitHub()))
    assert values == {"FACTORY_PR_WIP_LIMIT": "1", "FACTORY_MAIN_CI_WORKFLOWS": "rust.yml"}
