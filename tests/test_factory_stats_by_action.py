"""`rotisserie factory stats` shows each model's track record per action kind."""

from __future__ import annotations

from rotisserie.factory_console.commands.report import _by_action
from rotisserie.factory_console.github import Obj


def test_by_action_counts_prs_for_implement_and_completions_otherwise() -> None:
    kinds: Obj = {
        "review": {"runs": 5, "completed": 3, "delivered": 0},
        "implement": {"runs": 4, "completed": 3, "delivered": 1},
    }
    assert _by_action(kinds) == [("implement", 1, 4), ("review", 3, 5)]


def test_by_action_is_empty_without_kind_counts() -> None:
    assert _by_action({}) == []
