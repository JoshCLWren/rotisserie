#!/usr/bin/env python3
"""Regression coverage for manual-only factory governance."""
from __future__ import annotations

from factory_work_policy import build_candidates


def issue(
    number: int,
    title: str,
    *extra_labels: str,
    body: str = "",
) -> dict[str, object]:
    """Return a minimal unowned factory issue fixture."""
    return {
        "number": number,
        "state": "OPEN",
        "title": title,
        "body": body,
        "labels": [
            {"name": "factory"},
            {"name": "factory:unowned"},
            *({"name": label} for label in extra_labels),
        ],
        "createdAt": "2026-08-21T00:00:00Z",
    }


def test_epic_and_prd_are_not_ordinary_factory_candidates() -> None:
    """Parent product work stays outside autonomous implementation intake."""
    candidates = build_candidates(
        [
            issue(2001, "Epic: Deliver the next factory phase", "epic"),
            issue(2002, "PRD: Define the next product capability", "prd"),
        ],
        [],
    )

    assert candidates == []


def test_manual_only_marker_excludes_ordinary_issue() -> None:
    """Human and interactive gates can opt out without title heuristics."""
    candidates = build_candidates(
        [
            issue(
                2003,
                "Production acceptance gate",
                body="<!-- factory-execution:manual-only -->\nHuman-controlled cutover.",
            )
        ],
        [],
    )

    assert candidates == []


def test_frozen_corrective_child_remains_executable() -> None:
    """Scoped implementation children remain valid factory work."""
    candidates = build_candidates(
        [
            issue(
                2004,
                "Correct one frozen reader-workflow defect",
                "bug",
                body="Implement this frozen contract exactly. Refs #2363.",
            )
        ],
        [],
    )

    assert [candidate.number for candidate in candidates] == [2004]


def test_blocked_work_remains_ineligible() -> None:
    """Existing explicit blockers remain fail-closed."""
    candidates = build_candidates(
        [
            issue(2005, "Blocked ordinary work", "ralph-status:blocked"),
            issue(2006, "Human-gated ordinary work", "factory:blocked"),
        ],
        [],
    )

    assert candidates == []
