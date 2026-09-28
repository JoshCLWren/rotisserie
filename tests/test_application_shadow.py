from __future__ import annotations

import pytest

from rotisserie.application import (
    DecisionDimension,
    DecisionObservation,
    DecisionSnapshot,
    DivergenceKind,
    compare_decisions,
)


def observation(
    dimension: DecisionDimension,
    subject: str,
    outcome: str,
    *,
    reasons: tuple[str, ...] = (),
    rank: int | None = None,
) -> DecisionObservation:
    return DecisionObservation(dimension, subject, outcome, reasons, rank)


def test_matching_snapshots_have_a_versioned_machine_readable_report() -> None:
    decisions = (
        observation(DecisionDimension.ELIGIBILITY, "issue:10", "blocked", reasons=("dep",)),
        observation(DecisionDimension.RANKING, "issue:11", "selected", rank=0),
        observation(DecisionDimension.OWNERSHIP, "issue:11", "worker:7"),
        observation(DecisionDimension.REVIEW, "change:4", "pending"),
        observation(DecisionDimension.COMPLETION, "change:4", "not_ready"),
        observation(DecisionDimension.RECOVERY, "lease:2", "keep"),
    )
    baseline = DecisionSnapshot("legacy", "snapshot-a", decisions)
    candidate = DecisionSnapshot.from_dict({**baseline.to_dict(), "source": "rotisserie"})

    report = compare_decisions(baseline, candidate)

    assert report.matches
    assert report.to_dict() == {
        "schema_version": 1,
        "baseline": {"source": "legacy", "revision": "snapshot-a"},
        "candidate": {"source": "rotisserie", "revision": "snapshot-a"},
        "compared": 6,
        "matches": True,
        "divergences": [],
    }


def test_every_difference_is_explained_in_stable_order() -> None:
    baseline = DecisionSnapshot(
        "legacy",
        "old",
        (
            observation(DecisionDimension.RANKING, "issue:2", "selected", rank=0),
            observation(
                DecisionDimension.ELIGIBILITY,
                "issue:1",
                "blocked",
                reasons=("dependency",),
            ),
            observation(DecisionDimension.RECOVERY, "lease:gone", "release"),
        ),
    )
    candidate = DecisionSnapshot(
        "rotisserie",
        "old",
        (
            observation(DecisionDimension.RANKING, "issue:2", "selected", rank=1),
            observation(
                DecisionDimension.ELIGIBILITY,
                "issue:1",
                "eligible",
                reasons=("boundary",),
            ),
            observation(DecisionDimension.REVIEW, "change:new", "pending"),
        ),
    )

    report = compare_decisions(baseline, candidate)

    assert not report.matches
    assert [(item.dimension, item.subject, item.kind) for item in report.divergences] == [
        (DecisionDimension.ELIGIBILITY, "issue:1", DivergenceKind.OUTCOME),
        (DecisionDimension.ELIGIBILITY, "issue:1", DivergenceKind.REASONS),
        (DecisionDimension.RANKING, "issue:2", DivergenceKind.RANK),
        (DecisionDimension.RECOVERY, "lease:gone", DivergenceKind.MISSING_CANDIDATE),
        (DecisionDimension.REVIEW, "change:new", DivergenceKind.MISSING_BASELINE),
    ]
    assert report.to_dict()["compared"] == 4


def test_snapshot_rejects_ambiguous_or_invalid_records() -> None:
    item = observation(DecisionDimension.OWNERSHIP, "issue:1", "unowned")
    with pytest.raises(ValueError, match="must be unique"):
        DecisionSnapshot("legacy", "rev", (item, item))
    with pytest.raises(ValueError, match="ranking decisions require"):
        observation(DecisionDimension.RANKING, "issue:1", "selected")
    with pytest.raises(ValueError, match="only ranking"):
        observation(DecisionDimension.REVIEW, "change:1", "pending", rank=0)
    with pytest.raises(ValueError, match="schema version"):
        DecisionSnapshot.from_dict({"schema_version": 2})


def test_comparison_rejects_different_graph_revisions() -> None:
    baseline = DecisionSnapshot("legacy", "snapshot-a", ())
    candidate = DecisionSnapshot("rotisserie", "snapshot-b", ())

    with pytest.raises(ValueError, match="same graph revision"):
        compare_decisions(baseline, candidate)
