"""Shared fixtures for the post-merge closure tests."""

from __future__ import annotations


class ClosureIssueMixin:
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
