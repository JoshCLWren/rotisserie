"""Shared fixtures for the post-merge closure tests."""

from __future__ import annotations

from unittest import mock

import factory_post_merge_closure_github as github


class NoPersonReopenMixin:
    """Issues in these tests were never reopened by a person."""

    def setUp(self) -> None:
        patcher = mock.patch.object(github, "_reopened_by_person", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)  # type: ignore[attr-defined]
        super().setUp()  # type: ignore[misc]


class ClosureIssueMixin(NoPersonReopenMixin):
    def _closure_issue(
        self,
        number: int,
        *,
        body: str = "",
        state: str = "OPEN",
    ) -> dict[str, object]:
        """Build a minimal issue view for closure dependency tests."""
        return {
            "state": state,
            "body": body,
            "labels": ["enhancement", "factory", "factory:ready", "factory:unowned"],
        }
