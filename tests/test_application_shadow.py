from __future__ import annotations

import pytest

from rotisserie.application import (
    DecisionDimension,
    DecisionObservation,
    DecisionSnapshot,
    DivergenceKind,
    compare_decisions,
    project_decisions,
)
from rotisserie.domain import (
    Change,
    ChangeId,
    Check,
    CheckId,
    CheckStatus,
    GraphSnapshot,
    Lease,
    LeaseId,
    RepositoryId,
    Review,
    ReviewDecision,
    ReviewId,
    Revision,
    RevisionId,
    Work,
    Worker,
    WorkerId,
    WorkId,
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
    assert type(report).from_dict(report.to_dict()) == report
    assert report.to_dict() == {
        "schema_version": 1,
        "baseline": {"source": "legacy", "revision": "snapshot-a"},
        "candidate": {"source": "rotisserie", "revision": "snapshot-a"},
        "compared": 6,
        "dimensions": [
            "completion",
            "eligibility",
            "ownership",
            "ranking",
            "recovery",
            "review",
        ],
        "matches": True,
        "divergences": [],
    }


def test_graph_projection_emits_all_portable_decision_dimensions() -> None:
    repository = RepositoryId("example.test", "acme", "project")
    work = WorkId(repository, "1")
    change = ChangeId(repository, "2")
    revision = RevisionId(repository, "abc")
    producer = WorkerId("agent", "producer")
    reviewer = WorkerId("agent", "reviewer")
    snapshot = GraphSnapshot(
        works=(Work(work, "Ship it", priority=4),),
        changes=(Change(change, work, revision, producer),),
        revisions=(Revision(revision),),
        workers=(Worker(producer), Worker(reviewer)),
        leases=(Lease(LeaseId("old"), work, producer, 1, 5),),
        checks=(Check(CheckId(revision, "ci"), CheckStatus.PASSED),),
        reviews=(Review(ReviewId("review"), revision, reviewer, ReviewDecision.APPROVED),),
    )

    projected = project_decisions(snapshot, source="rotisserie", revision="host-7", at=10)

    assert projected.revision == "host-7"
    assert projected.dimensions == frozenset(DecisionDimension)
    assert [item.to_dict() for item in projected.observations] == [
        {
            "dimension": "eligibility",
            "subject": "work:1",
            "outcome": "blocked",
            "reasons": ["implementation_exists"],
            "rank": None,
        },
        {
            "dimension": "ownership",
            "subject": "work:1",
            "outcome": "unowned",
            "reasons": [],
            "rank": None,
        },
        {
            "dimension": "review",
            "subject": "change:2",
            "outcome": "approved",
            "reasons": [],
            "rank": None,
        },
        {
            "dimension": "completion",
            "subject": "change:2",
            "outcome": "ready",
            "reasons": [],
            "rank": None,
        },
        {
            "dimension": "recovery",
            "subject": "lease:old",
            "outcome": "release",
            "reasons": ["expired"],
            "rank": None,
        },
    ]


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


def test_shadow_report_deserialization_rejects_inconsistent_match_flag() -> None:
    report = compare_decisions(
        DecisionSnapshot("legacy", "snapshot-a", ()),
        DecisionSnapshot("rotisserie", "snapshot-a", ()),
    ).to_dict()
    report["matches"] = False

    from rotisserie.application import ShadowReport

    with pytest.raises(ValueError, match="matches flag"):
        ShadowReport.from_dict(report)
