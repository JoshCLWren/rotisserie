from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from rotisserie.application.controller import (
    AcquisitionFailure,
    ControllerView,
    DispatchOutcome,
    Liveness,
)
from rotisserie.application.coordination import EffectConflict, EffectFailure, EffectKind, GraphView
from rotisserie.application.review_controller import (
    ReviewAssignmentController,
    ReviewCandidate,
    ReviewEffect,
    ReviewRequest,
    ReviewStatus,
    ReviewView,
    plan_review,
)
from rotisserie.application.runtime import Assignment, AssignmentId, Authority, AuthorityManifest
from rotisserie.domain import (
    Boundary,
    BoundaryId,
    BoundaryKind,
    Change,
    ChangeId,
    GraphSnapshot,
    LeaseId,
    RepositoryId,
    ReviewIndependenceSignals,
    Revision,
    RevisionId,
    SchedulingPolicy,
    Work,
    Worker,
    WorkerId,
    WorkId,
)

REPO = RepositoryId("example.test", "org", "repo")
WORK = WorkId(REPO, "12")
CHANGE = ChangeId(REPO, "15")
HEAD = RevisionId(REPO, "a" * 40)
NEXT = RevisionId(REPO, "b" * 40)
PRODUCER = WorkerId("agent", "producer")
REPAIRER = WorkerId("agent", "repairer")
REVIEWER = WorkerId("agent", "reviewer")
ASSIGNMENT = Assignment(
    AssignmentId("review-15"),
    REVIEWER,
    "Review exact revision",
    AuthorityManifest(WORK, HEAD, frozenset({Authority.READ, Authority.REPORT_EVIDENCE}), 30),
    change=CHANGE,
)
REQUEST = ReviewRequest(
    REPO,
    ASSIGNMENT,
    LeaseId("review-lease"),
    "review-tick",
    SchedulingPolicy(3, 1, 5),
    10,
    0,
    frozenset({PRODUCER, REPAIRER, REVIEWER}),
)
CANDIDATE = ReviewCandidate(
    CHANGE,
    ReviewIndependenceSignals(
        HEAD,
        frozenset({PRODUCER, REPAIRER}),
        producer=PRODUCER,
        producer_attested=True,
        provenance_recorded=True,
    ),
    10,
    True,
    True,
)


class Port:
    def __init__(self) -> None:
        snapshot = GraphSnapshot(
            works=(Work(WORK, "Capability"),),
            changes=(Change(CHANGE, WORK, HEAD, PRODUCER),),
            revisions=(Revision(HEAD), Revision(NEXT)),
            workers=tuple(Worker(w) for w in sorted(REQUEST.workers)),
        )
        self.view = ReviewView(
            ControllerView(
                GraphView(snapshot, "1"),
                10,
                0,
                0,
                tuple((w, Liveness.IDLE) for w in sorted(REQUEST.workers)),
            ),
            (CANDIDATE,),
            True,
        )
        self.commands: list[ReviewEffect] = []
        self.assignments: list[Assignment] = []
        self.reads = 0
        self.outcome = DispatchOutcome.ACCEPTED
        self.on_read: Callable[[Port], None] = lambda p: None
        self.on_effect: Callable[[Port, ReviewEffect], None] = lambda p, e: None
        self.on_dispatch: Callable[[Port], None] = lambda p: None
        self.fail: Exception | None = None

    def acquire(self) -> ReviewView:
        self.reads += 1
        self.on_read(self)
        return self.view

    def change(self, **kwargs: Any) -> None:
        self.view = replace(self.view, **kwargs)
        c = self.view.controller
        self.view = replace(
            self.view,
            controller=replace(c, graph=replace(c.graph, version=str(int(c.graph.version) + 1))),
        )

    def apply(self, effect: ReviewEffect) -> None:
        self.commands.append(effect)
        self.on_effect(self, effect)
        if effect.command.expected_version != self.view.controller.graph.version:
            raise EffectConflict("changed view")
        if self.fail:
            raise self.fail
        c = self.view.controller
        graph = c.graph
        binding = effect.binding
        if effect.command.kind is EffectKind.CLAIM:
            assert plan_review(self.view, effect.request).binding == binding
            self.change(
                bindings=(*self.view.bindings, binding),
                controller=replace(
                    c,
                    active=c.active + 1,
                    graph=replace(
                        graph,
                        snapshot=replace(
                            graph.snapshot, leases=(*graph.snapshot.leases, binding.lease)
                        ),
                    ),
                ),
            )
        else:
            assert binding in self.view.bindings and binding.lease in graph.snapshot.leases
            self.change(
                bindings=tuple(b for b in self.view.bindings if b != binding),
                controller=replace(
                    c,
                    active=c.active - 1,
                    graph=replace(
                        graph,
                        snapshot=replace(
                            graph.snapshot,
                            leases=tuple(x for x in graph.snapshot.leases if x != binding.lease),
                        ),
                    ),
                ),
            )

    def dispatch(self, effect: ReviewEffect) -> DispatchOutcome:
        self.commands.append(effect)
        self.on_effect(self, effect)
        if effect.command.expected_version != self.view.controller.graph.version:
            raise EffectConflict("changed view")
        assert effect.binding in self.view.bindings
        if self.outcome is DispatchOutcome.ACCEPTED:
            self.assignments.append(effect.binding.assignment)
        self.on_dispatch(self)
        return self.outcome


def test_plan_has_no_effects_and_versioned_exact_binding() -> None:
    port = Port()
    result = ReviewAssignmentController(port).tick(REQUEST)
    assert result.status is ReviewStatus.SELECTED
    assert result.binding and result.binding.candidate.signals.revision == HEAD
    assert result.to_dict()["schema_version"] == 1
    assert port.reads == 1 and not port.commands and not port.assignments


def test_atomic_review_request_handoff_and_duplicate_suppression() -> None:
    port = Port()
    controller = ReviewAssignmentController(port)
    assert controller.tick(REQUEST, apply=True).status is ReviewStatus.DISPATCHED
    assert port.assignments == [ASSIGNMENT]
    assert controller.tick(REQUEST, apply=True).status is ReviewStatus.BUSY
    assert [x.command.kind for x in port.commands] == [EffectKind.CLAIM, EffectKind.DISPATCH]
    for effect in port.commands:
        assert effect.command.change == CHANGE and effect.command.expected_revision == HEAD
        assert effect.command.lease == effect.binding.lease
        assert effect.command.lease_id == REQUEST.lease_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("complete", False),
        ("trusted", False),
        ("observed_at", 9),
        ("observed_at", 11),
    ],
)
def test_incomplete_untrusted_stale_future_provenance(field: str, value: Any) -> None:
    port = Port()
    port.change(candidates=(replace(CANDIDATE, **{field: value}),))
    assert (
        ReviewAssignmentController(port).tick(REQUEST, apply=True).status is ReviewStatus.PROVENANCE
    )
    assert not port.commands


@pytest.mark.parametrize(
    "signals",
    [
        replace(CANDIDATE.signals, revision=NEXT),
        replace(CANDIDATE.signals, provenance_recorded=False),
        replace(CANDIDATE.signals, producer_attested=False),
        replace(CANDIDATE.signals, producer_attested=False, producer=None),
        replace(CANDIDATE.signals, producer=REPAIRER),
        replace(CANDIDATE.signals, contributors=frozenset({REPAIRER})),
        replace(
            CANDIDATE.signals, contributors=frozenset({PRODUCER, WorkerId("agent", "unknown")})
        ),
    ],
)
def test_provenance_holds_even_with_multiple_approvers(signals: ReviewIndependenceSignals) -> None:
    port = Port()
    port.change(
        candidates=(
            replace(CANDIDATE, signals=replace(signals, approvers=frozenset({REVIEWER, REPAIRER}))),
        )
    )
    assert (
        ReviewAssignmentController(port).tick(REQUEST, apply=True).status is ReviewStatus.PROVENANCE
    )
    assert not port.commands


@pytest.mark.parametrize("worker", [PRODUCER, REPAIRER])
def test_all_contributors_excluded(worker: WorkerId) -> None:
    port = Port()
    request = replace(REQUEST, assignment=replace(ASSIGNMENT, worker=worker))
    assert ReviewAssignmentController(port).tick(request).status is ReviewStatus.ZERO


def mutate_head(port: Port) -> None:
    c = port.view.controller
    port.change(
        controller=replace(
            c, graph=replace(c.graph, snapshot=c.graph.snapshot.with_change_head(CHANGE, NEXT))
        )
    )


def mutate_contributors(port: Port) -> None:
    port.change(
        candidates=(
            replace(
                CANDIDATE,
                signals=replace(
                    CANDIDATE.signals, contributors=frozenset({PRODUCER, REPAIRER, REVIEWER})
                ),
            ),
        )
    )


def mutate_worker(port: Port) -> None:
    port.change(
        controller=replace(
            port.view.controller,
            liveness=(
                (PRODUCER, Liveness.ACTIVE),
                (REPAIRER, Liveness.IDLE),
                (REVIEWER, Liveness.IDLE),
            ),
        )
    )


def mutate_gate(port: Port) -> None:
    c = port.view.controller
    port.change(
        controller=replace(
            c,
            graph=replace(
                c.graph,
                snapshot=replace(
                    c.graph.snapshot,
                    boundaries=(Boundary(BoundaryId("human"), WORK, BoundaryKind.HUMAN_APPROVAL),),
                ),
            ),
        )
    )


def mutate_lease(port: Port) -> None:
    c = port.view.controller
    lease = replace(c.graph.snapshot.leases[0], expires_at=40)
    port.change(
        controller=replace(
            c, graph=replace(c.graph, snapshot=replace(c.graph.snapshot, leases=(lease,)))
        )
    )


@pytest.mark.parametrize("mutation", [mutate_head, mutate_contributors, mutate_worker, mutate_gate])
@pytest.mark.parametrize("read", [2, 3])
def test_reacquisition_fences_concurrent_changes(
    mutation: Callable[[Port], None], read: int
) -> None:
    port = Port()
    port.on_read = lambda p: mutation(p) if p.reads == read else None
    result = ReviewAssignmentController(port).tick(REQUEST, apply=True)
    assert result.status not in {ReviewStatus.SELECTED, ReviewStatus.DISPATCHED, ReviewStatus.ZERO}
    assert not port.assignments
    assert len(port.commands) == (1 if read == 3 else 0)


@pytest.mark.parametrize("kind", [EffectKind.CLAIM, EffectKind.DISPATCH, EffectKind.RELEASE])
def test_cas_races_at_each_effect_boundary(kind: EffectKind) -> None:
    port = Port()
    port.outcome = DispatchOutcome.REFUSED
    port.on_effect = lambda p, e: mutate_worker(p) if e.command.kind is kind else None
    result = ReviewAssignmentController(port).tick(REQUEST, apply=True)
    assert result.status in {ReviewStatus.STALE, ReviewStatus.RELEASE_HELD}
    assert not port.assignments
    if kind is not EffectKind.CLAIM:
        assert port.view.controller.graph.snapshot.leases


@pytest.mark.parametrize(
    "mutation", [mutate_head, mutate_contributors, mutate_worker, mutate_gate, mutate_lease]
)
def test_refusal_compensation_preserves_changed_ownership(mutation: Callable[[Port], None]) -> None:
    port = Port()
    port.outcome = DispatchOutcome.REFUSED
    port.on_dispatch = mutation
    assert (
        ReviewAssignmentController(port).tick(REQUEST, apply=True).status
        is ReviewStatus.RELEASE_HELD
    )
    assert port.view.controller.graph.snapshot.leases
    assert [e.command.kind for e in port.commands] == [EffectKind.CLAIM, EffectKind.DISPATCH]


@pytest.mark.parametrize(
    "outcome,status",
    [
        (DispatchOutcome.REFUSED, ReviewStatus.RELEASED),
        (DispatchOutcome.UNKNOWN, ReviewStatus.DISPATCH_UNCERTAIN),
    ],
)
def test_only_proven_refusal_compensates(outcome: DispatchOutcome, status: ReviewStatus) -> None:
    port = Port()
    port.outcome = outcome
    assert ReviewAssignmentController(port).tick(REQUEST, apply=True).status is status
    assert bool(port.view.bindings) == (outcome is DispatchOutcome.UNKNOWN)


@pytest.mark.parametrize(
    "failure,status",
    [
        (EffectFailure("refused"), ReviewStatus.CLAIM_FAILED),
        (ConnectionError("lost acknowledgement"), ReviewStatus.CLAIM_UNCERTAIN),
    ],
)
def test_claim_failures_do_not_dispatch(failure: Exception, status: ReviewStatus) -> None:
    port = Port()
    port.fail = failure
    assert ReviewAssignmentController(port).tick(REQUEST, apply=True).status is status
    assert not port.assignments


def test_acquisition_capacity_backpressure_liveness_and_zero_are_distinct() -> None:
    port = Port()
    port.change(controller=replace(port.view.controller, active=3))
    assert plan_review(port.view, REQUEST).status is ReviewStatus.CAPACITY
    port.change(controller=replace(port.view.controller, active=0, completion_backlog=5))
    assert plan_review(port.view, REQUEST).status is ReviewStatus.BACKPRESSURE
    port.change(candidates=())
    assert plan_review(port.view, REQUEST).status is ReviewStatus.ZERO
    port.change(liveness_complete=False)
    assert plan_review(port.view, REQUEST).status is ReviewStatus.LIVENESS
    port.change(controller=replace(port.view.controller, observed_at=9))
    assert plan_review(port.view, REQUEST).status is ReviewStatus.ACQUISITION

    def failed(p: Port) -> None:
        raise AcquisitionFailure("incomplete")

    port.on_read = failed
    assert ReviewAssignmentController(port).tick(REQUEST).status is ReviewStatus.ACQUISITION


def test_changed_assignment_changes_claim_and_handoff_keys() -> None:
    first, second = Port(), Port()
    ReviewAssignmentController(first).tick(REQUEST, apply=True)
    ReviewAssignmentController(second).tick(
        replace(
            REQUEST, assignment=replace(ASSIGNMENT, objective="Different trusted review objective")
        ),
        apply=True,
    )
    assert all(
        a.command.idempotency_key != b.command.idempotency_key
        for a, b in zip(first.commands, second.commands, strict=True)
    )


def test_changed_head_never_uses_old_assignment_or_approvals() -> None:
    from rotisserie.domain import Review, ReviewDecision, ReviewId

    port = Port()
    mutate_head(port)
    c = port.view.controller
    port.change(
        controller=replace(
            c,
            graph=replace(
                c.graph,
                snapshot=replace(
                    c.graph.snapshot,
                    reviews=(Review(ReviewId("formal"), NEXT, REVIEWER, ReviewDecision.APPROVED),),
                ),
            ),
        ),
        candidates=(replace(CANDIDATE, signals=replace(CANDIDATE.signals, revision=NEXT)),),
    )
    assert plan_review(port.view, REQUEST).status is ReviewStatus.STALE
    port.change(
        candidates=(
            replace(
                CANDIDATE,
                signals=replace(
                    CANDIDATE.signals,
                    revision=NEXT,
                    producer_attested=False,
                    provenance_recorded=False,
                ),
            ),
        )
    )
    assert plan_review(port.view, REQUEST).status is ReviewStatus.PROVENANCE
    assert not port.commands


def test_multiple_candidates_follow_explicit_order_and_handoff_only_one() -> None:
    port = Port()
    second_work = WorkId(REPO, "13")
    second_change = ChangeId(REPO, "16")
    c = port.view.controller
    port.change(
        controller=replace(
            c,
            graph=replace(
                c.graph,
                snapshot=replace(
                    c.graph.snapshot,
                    works=(*c.graph.snapshot.works, Work(second_work, "Other")),
                    changes=(
                        *c.graph.snapshot.changes,
                        Change(second_change, second_work, HEAD, PRODUCER),
                    ),
                ),
            ),
        ),
        candidates=(CANDIDATE, replace(CANDIDATE, change=second_change)),
    )
    assert (
        ReviewAssignmentController(port).tick(REQUEST, apply=True).status is ReviewStatus.DISPATCHED
    )
    assert len(port.assignments) == 1
    other = replace(
        REQUEST,
        assignment=replace(
            ASSIGNMENT,
            change=second_change,
            authority=replace(ASSIGNMENT.authority, work=second_work),
        ),
    )
    fresh = Port()
    fresh.view = replace(
        port.view,
        bindings=(),
        controller=replace(
            port.view.controller,
            active=0,
            graph=replace(
                port.view.controller.graph,
                snapshot=replace(port.view.controller.graph.snapshot, leases=()),
            ),
        ),
    )
    assert plan_review(fresh.view, other).status is ReviewStatus.STALE


def test_unacknowledged_claim_and_dispatch_keep_exact_ownership() -> None:
    class LostClaimPort(Port):
        def apply(self, effect: ReviewEffect) -> None:
            super().apply(effect)
            raise ConnectionError("lost claim acknowledgement")

    claim = LostClaimPort()
    assert (
        ReviewAssignmentController(claim).tick(REQUEST, apply=True).status
        is ReviewStatus.CLAIM_UNCERTAIN
    )
    assert (
        claim.view.bindings
        and claim.view.controller.graph.snapshot.leases
        and not claim.assignments
    )

    class LostDispatchPort(Port):
        def dispatch(self, effect: ReviewEffect) -> DispatchOutcome:
            super().dispatch(effect)
            raise ConnectionError("lost handoff acknowledgement")

    dispatch = LostDispatchPort()
    assert (
        ReviewAssignmentController(dispatch).tick(REQUEST, apply=True).status
        is ReviewStatus.DISPATCH_UNCERTAIN
    )
    assert dispatch.view.bindings and dispatch.assignments == [ASSIGNMENT]
    assert all(e.command.kind is not EffectKind.RELEASE for e in dispatch.commands)


@pytest.mark.parametrize("mutation", ["replace", "missing_binding", "capacity", "backlog"])
def test_post_claim_exact_binding_and_capacity_required(mutation: str) -> None:
    port = Port()

    def change(p: Port) -> None:
        if p.reads != 3:
            return
        if mutation == "missing_binding":
            p.change(bindings=())
        elif mutation == "replace":
            c = p.view.controller
            p.change(
                controller=replace(
                    c,
                    graph=replace(
                        c.graph,
                        snapshot=replace(
                            c.graph.snapshot,
                            leases=(
                                replace(c.graph.snapshot.leases[0], id=LeaseId("replacement")),
                            ),
                        ),
                    ),
                )
            )
        elif mutation == "capacity":
            p.change(controller=replace(p.view.controller, active=3))
        else:
            p.change(controller=replace(p.view.controller, completion_backlog=5))

    port.on_read = change
    assert ReviewAssignmentController(port).tick(REQUEST, apply=True).status is ReviewStatus.STALE
    assert not port.assignments


def test_expired_lease_holds_for_recovery_and_unknown_liveness_is_never_zero() -> None:
    from rotisserie.domain import Lease

    port = Port()
    c = port.view.controller
    port.change(
        controller=replace(
            c,
            graph=replace(
                c.graph,
                snapshot=replace(
                    c.graph.snapshot, leases=(Lease(LeaseId("expired"), WORK, PRODUCER, 1, 5),)
                ),
            ),
        )
    )
    assert plan_review(port.view, REQUEST).status is ReviewStatus.RECOVERY
    port.change(
        controller=replace(port.view.controller, liveness=((REVIEWER, Liveness.IDLE),)),
        candidates=(),
    )
    assert plan_review(port.view, REQUEST).status is ReviewStatus.LIVENESS


def test_human_gate_and_explicit_prerequisite_hold() -> None:
    from rotisserie.domain import WorkState

    port = Port()
    mutate_gate(port)
    assert plan_review(port.view, REQUEST).status is ReviewStatus.ZERO
    prerequisite = WorkId(REPO, "11")
    c = port.view.controller
    port.change(
        controller=replace(
            c,
            graph=replace(
                c.graph,
                snapshot=replace(
                    c.graph.snapshot,
                    boundaries=(),
                    works=(*c.graph.snapshot.works, Work(prerequisite, "Required")),
                    dependencies=frozenset({(WORK, prerequisite)}),
                ),
            ),
        )
    )
    assert plan_review(port.view, REQUEST).status is ReviewStatus.ZERO
    c = port.view.controller
    port.change(
        controller=replace(
            c,
            graph=replace(
                c.graph,
                snapshot=replace(
                    c.graph.snapshot,
                    works=tuple(
                        replace(w, state=WorkState.COMPLETED) if w.id == prerequisite else w
                        for w in c.graph.snapshot.works
                    ),
                ),
            ),
        )
    )
    assert plan_review(port.view, REQUEST).status is ReviewStatus.SELECTED


def test_request_rejects_missing_exact_head_expired_and_unconfigured_authority() -> None:
    with pytest.raises(ValueError):
        replace(REQUEST, assignment=replace(ASSIGNMENT, change=None))
    with pytest.raises(ValueError):
        replace(REQUEST, at=30)
    with pytest.raises(ValueError):
        replace(REQUEST, workers=frozenset())
