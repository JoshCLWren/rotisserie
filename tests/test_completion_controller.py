from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rotisserie.application.completion_controller import (
    CompletionController,
    CompletionEffect,
    CompletionPhase,
    CompletionRecord,
    CompletionStatus,
    CompletionView,
    plan_completion,
)
from rotisserie.application.controller import AcquisitionFailure, Liveness
from rotisserie.application.coordination import EffectConflict, EffectFailure, EffectKind
from rotisserie.domain import (
    Boundary,
    BoundaryId,
    BoundaryKind,
    Change,
    ChangeId,
    CheckStatus,
    Lease,
    LeaseId,
    Review,
    ReviewDecision,
    ReviewId,
    RevisionId,
    Work,
    WorkId,
    WorkState,
)
from rotisserie.operator.completion import completion_request_from_dict, completion_view_from_dict

EXAMPLE = Path(__file__).parents[1] / "examples/controller/completion"
REQUEST = completion_request_from_dict(json.loads((EXAMPLE / "request.json").read_text()))
VIEW = completion_view_from_dict(json.loads((EXAMPLE / "view.json").read_text()))
EVIDENCE = VIEW.candidates[0]
HEAD = REQUEST.revision
NEXT = RevisionId(REQUEST.repository, "b" * 40)
WORK = VIEW.controller.graph.snapshot.changes[0].work
producer = EVIDENCE.provenance.signals.producer
assert producer is not None
PRODUCER = producer
REPAIRER = next(x for x in EVIDENCE.provenance.signals.contributors if x != PRODUCER)


class Port:
    """Contract fake: full-view CAS, injected clock, durable receipt, actual head fence."""

    def __init__(self) -> None:
        self.view = VIEW
        self.reads = 0
        self.commands: list[CompletionEffect] = []
        self.merged: list[RevisionId] = []
        self.remote_head = HEAD
        self.now = REQUEST.at
        self.phase = CompletionPhase.COMPLETED
        self.on_read: Callable[[Port], None] = lambda p: None
        self.on_effect: Callable[[Port, CompletionEffect], None] = lambda p, e: None
        self.before_merge: Callable[[Port], None] = lambda p: None
        self.claim_error: Exception | None = None
        self.lose_claim_ack = False
        self.lose_merge_ack = False

    def acquire(self) -> CompletionView:
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

    def fence(self, e: CompletionEffect) -> None:
        self.commands.append(e)
        self.on_effect(self, e)
        r = e.binding.request
        if (
            e.command.expected_version != self.view.controller.graph.version
            or not r.at <= self.now < e.binding.lease.expires_at
            or not 0 <= self.now - self.view.controller.observed_at <= r.max_observation_age
        ):
            raise EffectConflict("CAS or current clock refused")

    def claim(self, e: CompletionEffect) -> None:
        self.fence(e)
        if self.claim_error:
            raise self.claim_error
        assert plan_completion(self.view, e.binding.request).binding == e.binding
        c = self.view.controller
        self.change(
            records=(*self.view.records, CompletionRecord(e.binding)),
            controller=replace(
                c,
                active=c.active + 1,
                graph=replace(
                    c.graph,
                    snapshot=replace(
                        c.graph.snapshot, leases=(*c.graph.snapshot.leases, e.binding.lease)
                    ),
                ),
            ),
        )
        if self.lose_claim_ack:
            raise ConnectionError("lost claim ack")

    def complete(self, e: CompletionEffect) -> CompletionPhase:
        self.fence(e)
        assert CompletionRecord(e.binding) in self.view.records
        assert e.binding.lease in self.view.controller.graph.snapshot.leases
        assert e.command.expected_revision == e.binding.request.revision
        # This durable transition precedes any remote effect.
        self.change(records=(CompletionRecord(e.binding, CompletionPhase.ATTEMPTING),))
        merge_version = self.view.controller.graph.version
        self.before_merge(self)
        # Simulate the host's atomic expected-head merge guard independently of acquisition.
        phase = (
            self.phase
            if (
                self.remote_head == e.command.expected_revision
                and self.view.controller.graph.version == merge_version
                and self.now < e.binding.lease.expires_at
            )
            else CompletionPhase.REFUSED
        )
        if phase is CompletionPhase.COMPLETED:
            self.merged.append(self.remote_head)
        if self.lose_merge_ack:
            # No final acknowledgement: the durable ATTEMPTING state remains ambiguous.
            raise ConnectionError("lost merge ack")
        self.change(records=(CompletionRecord(e.binding, phase),))
        return phase


def test_planning_no_effects_and_bounded_completion_keeps_work_open() -> None:
    port = Port()
    assert CompletionController(port).tick(REQUEST).status is CompletionStatus.SELECTED
    assert port.reads == 1 and not port.commands and not port.merged
    result = CompletionController(port).tick(REQUEST, apply=True)
    assert result.status is CompletionStatus.COMPLETED and result.to_dict()["schema_version"] == 1
    assert port.merged == [HEAD]
    assert port.view.records[0].phase is CompletionPhase.COMPLETED
    assert port.view.controller.graph.snapshot.works[0].state is WorkState.OPEN
    assert [x.command.kind for x in port.commands] == [EffectKind.CLAIM, EffectKind.COMPLETE]
    for e in port.commands:
        assert e.command.expected_revision == HEAD and e.command.change == REQUEST.change
        assert e.command.lease == e.binding.lease
    assert CompletionController(port).tick(REQUEST, apply=True).status is CompletionStatus.OWNERSHIP
    assert len(port.merged) == 1


@pytest.mark.parametrize(
    "field,value",
    [("complete", False), ("trusted", False), ("observed_at", 9), ("observed_at", 11)],
)
def test_provenance_attestation_holds(field: str, value: Any) -> None:
    e = replace(EVIDENCE, provenance=replace(EVIDENCE.provenance, **{field: value}))
    assert (
        plan_completion(replace(VIEW, candidates=(e,)), REQUEST).status
        is CompletionStatus.PROVENANCE
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", NEXT),
        ("producer_attested", False),
        ("provenance_recorded", False),
        ("producer", REPAIRER),
        ("contributors", frozenset({REPAIRER})),
    ],
)
def test_missing_stale_contradictory_provenance_even_with_reviews(field: str, value: Any) -> None:
    p = EVIDENCE.provenance
    e = replace(EVIDENCE, provenance=replace(p, signals=replace(p.signals, **{field: value})))
    assert (
        plan_completion(replace(VIEW, candidates=(e,)), REQUEST).status
        is CompletionStatus.PROVENANCE
    )


@pytest.mark.parametrize("reviewer", [PRODUCER, REPAIRER])
def test_current_contributor_rule_excludes_prior_repairers(reviewer: Any) -> None:
    p = EVIDENCE.provenance
    e = replace(
        EVIDENCE,
        provenance=replace(p, signals=replace(p.signals, approvers=frozenset({reviewer}))),
        reviews=(replace(EVIDENCE.reviews[0], reviewer=reviewer),),
    )
    assert (
        plan_completion(replace(VIEW, candidates=(e,)), REQUEST).status is CompletionStatus.REVIEW
    )


def test_formal_approval_and_legacy_readiness_cannot_grant_completion() -> None:
    c = VIEW.controller
    s = replace(
        c.graph.snapshot,
        reviews=(Review(ReviewId("formal"), HEAD, REQUEST.worker, ReviewDecision.APPROVED),),
        checks=EVIDENCE.ci,
    )
    e = replace(
        EVIDENCE,
        reviews=(),
        provenance=replace(
            EVIDENCE.provenance, signals=replace(EVIDENCE.provenance.signals, approvers=frozenset())
        ),
    )
    v = replace(VIEW, candidates=(e,), controller=replace(c, graph=replace(c.graph, snapshot=s)))
    from rotisserie.domain import readiness_decision

    assert readiness_decision(s, REQUEST.change).ready
    assert plan_completion(v, REQUEST).status is CompletionStatus.REVIEW


@pytest.mark.parametrize(
    "lane,status", [("ci", CompletionStatus.CI), ("readiness", CompletionStatus.READINESS)]
)
@pytest.mark.parametrize(
    "mutation", ["missing", "pending", "failed", "stale", "not_required", "wrong_name"]
)
def test_all_required_exact_head_gates(lane: str, status: CompletionStatus, mutation: str) -> None:
    check = getattr(EVIDENCE, lane)[0]
    checks = {
        "missing": (),
        "pending": (replace(check, status=CheckStatus.PENDING),),
        "failed": (replace(check, status=CheckStatus.FAILED),),
        "stale": (replace(check, id=replace(check.id, revision=NEXT)),),
        "not_required": (replace(check, required=False),),
        "wrong_name": (replace(check, id=replace(check.id, name="other")),),
    }[mutation]
    assert (
        plan_completion(
            replace(
                VIEW,
                candidates=(
                    replace(EVIDENCE, ci=checks)
                    if lane == "ci"
                    else replace(EVIDENCE, readiness=checks),
                ),
            ),
            REQUEST,
        ).status
        is status
    )


@pytest.mark.parametrize(
    "mutation", ["missing", "stale", "rejected", "pending", "approver_mismatch"]
)
def test_semantic_review_evidence(mutation: str) -> None:
    r = EVIDENCE.reviews[0]
    reviews = {
        "missing": (),
        "stale": (replace(r, revision=NEXT),),
        "rejected": (replace(r, decision=ReviewDecision.CHANGES_REQUESTED),),
        "pending": (replace(r, decision=ReviewDecision.PENDING),),
        "approver_mismatch": (replace(r, reviewer=REPAIRER),),
    }[mutation]
    assert (
        plan_completion(
            replace(VIEW, candidates=(replace(EVIDENCE, reviews=reviews),)), REQUEST
        ).status
        is CompletionStatus.REVIEW
    )


@pytest.mark.parametrize(
    "field,value",
    [("trusted", False), ("complete", False), ("observed_at", 9), ("observed_at", 11)],
)
def test_evidence_acquisition_attestation(field: str, value: Any) -> None:
    assert (
        plan_completion(
            replace(VIEW, candidates=(replace(EVIDENCE, **{field: value}),)), REQUEST
        ).status
        is CompletionStatus.READINESS
    )


def mutate(port: Port, kind: str) -> None:
    c = port.view.controller
    s = c.graph.snapshot
    if kind == "head":
        port.change(
            controller=replace(
                c, graph=replace(c.graph, snapshot=s.with_change_head(REQUEST.change, NEXT))
            )
        )
    elif kind in {"contributors", "reviews", "ci", "readiness"}:
        e = port.view.candidates[0]
        if kind == "contributors":
            p = e.provenance
            e = replace(
                e,
                provenance=replace(
                    p,
                    signals=replace(
                        p.signals, contributors=p.signals.contributors | {REQUEST.worker}
                    ),
                ),
            )
        elif kind == "reviews":
            e = replace(
                e, reviews=(replace(e.reviews[0], decision=ReviewDecision.CHANGES_REQUESTED),)
            )
        else:
            checks = getattr(e, kind)
            failed = (replace(checks[0], status=CheckStatus.FAILED),)
            e = replace(e, ci=failed) if kind == "ci" else replace(e, readiness=failed)
        port.change(candidates=(e,))
    elif kind == "human":
        port.change(
            controller=replace(
                c,
                graph=replace(
                    c.graph,
                    snapshot=replace(
                        s,
                        boundaries=(
                            Boundary(BoundaryId("human"), WORK, BoundaryKind.HUMAN_ACTION),
                        ),
                    ),
                ),
            )
        )
    elif kind == "worker":
        port.change(
            controller=replace(
                c,
                liveness=tuple(
                    (w, Liveness.ACTIVE if w == PRODUCER else state) for w, state in c.liveness
                ),
            )
        )
    elif kind in {"renewal", "replacement"}:
        lease = s.leases[0]
        lease = (
            replace(lease, expires_at=40)
            if kind == "renewal"
            else replace(lease, id=LeaseId("replacement"))
        )
        port.change(
            controller=replace(c, graph=replace(c.graph, snapshot=replace(s, leases=(lease,))))
        )
    elif kind == "record":
        port.change(records=())
    elif kind == "phase":
        port.change(records=(replace(port.view.records[0], phase=CompletionPhase.UNKNOWN),))
    elif kind == "capacity":
        port.change(controller=replace(c, active=3))
    elif kind == "backlog":
        port.change(controller=replace(c, completion_backlog=5))
    elif kind == "freshness":
        port.change(controller=replace(c, observed_at=9))
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind",
    [
        "head",
        "contributors",
        "reviews",
        "ci",
        "readiness",
        "human",
        "worker",
        "capacity",
        "backlog",
        "freshness",
    ],
)
@pytest.mark.parametrize("read", [2, 3])
def test_reacquire_every_boundary(kind: str, read: int) -> None:
    port = Port()
    port.on_read = lambda p: mutate(p, kind) if p.reads == read else None
    result = CompletionController(port).tick(REQUEST, apply=True)
    assert result.status is CompletionStatus.STALE
    assert not port.merged and len(port.commands) == (1 if read == 3 else 0)


@pytest.mark.parametrize("kind", ["renewal", "replacement", "record", "phase"])
def test_full_claimed_ownership_is_required(kind: str) -> None:
    port = Port()
    port.on_read = lambda p: mutate(p, kind) if p.reads == 3 else None
    assert CompletionController(port).tick(REQUEST, apply=True).status is CompletionStatus.STALE
    assert not port.merged


@pytest.mark.parametrize(
    "kind",
    [
        "head",
        "contributors",
        "reviews",
        "ci",
        "readiness",
        "human",
        "worker",
        "capacity",
        "backlog",
    ],
)
@pytest.mark.parametrize("boundary", [EffectKind.CLAIM, EffectKind.COMPLETE])
def test_adapter_full_view_cas_races(kind: str, boundary: EffectKind) -> None:
    port = Port()
    port.on_effect = lambda p, e: mutate(p, kind) if e.command.kind is boundary else None
    assert CompletionController(port).tick(REQUEST, apply=True).status is CompletionStatus.STALE
    assert not port.merged


def test_actual_merge_head_fence_after_final_acquisition() -> None:
    port = Port()
    port.before_merge = lambda p: setattr(p, "remote_head", NEXT)
    assert CompletionController(port).tick(REQUEST, apply=True).status is CompletionStatus.REFUSED
    assert not port.merged and port.view.records[0].phase is CompletionPhase.REFUSED
    assert port.view.controller.graph.snapshot.leases


@pytest.mark.parametrize(
    "phase,status",
    [
        (CompletionPhase.COMPLETED, CompletionStatus.COMPLETED),
        (CompletionPhase.REFUSED, CompletionStatus.REFUSED),
        (CompletionPhase.FAILED, CompletionStatus.FAILED),
        (CompletionPhase.UNKNOWN, CompletionStatus.UNCERTAIN),
        (CompletionPhase.ATTEMPTING, CompletionStatus.UNCERTAIN),
    ],
)
def test_durable_outcomes_no_compensating_release_or_replay(
    phase: CompletionPhase, status: CompletionStatus
) -> None:
    port = Port()
    port.phase = phase
    result = CompletionController(port).tick(REQUEST, apply=True)
    assert result.status is status and len(result.commands) == 2
    assert port.view.records[0].phase is phase
    assert port.view.controller.graph.snapshot.leases
    assert (
        CompletionController(port).tick(replace(REQUEST, at=31), apply=True).status
        is CompletionStatus.ACQUISITION
    )
    # Even fresh reacquisition and a new operation/lease after expiry cannot discard the receipt.
    port.change(
        controller=replace(port.view.controller, observed_at=31),
        candidates=(
            replace(
                EVIDENCE, observed_at=31, provenance=replace(EVIDENCE.provenance, observed_at=31)
            ),
        ),
    )
    assert (
        CompletionController(port)
        .tick(replace(REQUEST, at=31, operation="retry", lease_id=LeaseId("retry")), apply=True)
        .status
        is CompletionStatus.OWNERSHIP
    )
    assert len(port.commands) == 2


@pytest.mark.parametrize("lost", ["claim", "merge"])
def test_lost_acknowledgements_are_durable_and_never_replayed(lost: str) -> None:
    port = Port()
    port.lose_claim_ack = lost == "claim"
    port.lose_merge_ack = lost == "merge"
    result = CompletionController(port).tick(REQUEST, apply=True)
    assert result.status is (
        CompletionStatus.CLAIM_UNCERTAIN if lost == "claim" else CompletionStatus.UNCERTAIN
    )
    assert port.view.records[0].phase is (
        CompletionPhase.CLAIMED if lost == "claim" else CompletionPhase.ATTEMPTING
    )
    assert len(port.merged) == (0 if lost == "claim" else 1)
    assert CompletionController(port).tick(REQUEST, apply=True).status is CompletionStatus.OWNERSHIP


@pytest.mark.parametrize(
    "error,status",
    [
        (EffectConflict("race"), CompletionStatus.STALE),
        (EffectFailure("proven unapplied"), CompletionStatus.CLAIM_FAILED),
        (ConnectionError("unknown"), CompletionStatus.CLAIM_UNCERTAIN),
    ],
)
def test_claim_failure_categories(error: Exception, status: CompletionStatus) -> None:
    port = Port()
    port.claim_error = error
    assert CompletionController(port).tick(REQUEST, apply=True).status is status
    assert not port.merged


def test_zero_holds_and_explicit_capacity_inputs() -> None:
    assert plan_completion(replace(VIEW, candidates=()), REQUEST).status is CompletionStatus.ZERO
    assert (
        plan_completion(replace(VIEW, candidates=(), liveness_complete=False), REQUEST).status
        is CompletionStatus.LIVENESS
    )
    assert (
        plan_completion(
            replace(VIEW, controller=replace(VIEW.controller, observed_at=9)), REQUEST
        ).status
        is CompletionStatus.ACQUISITION
    )
    assert (
        plan_completion(
            replace(VIEW, controller=replace(VIEW.controller, active=3)), REQUEST
        ).status
        is CompletionStatus.CAPACITY
    )
    assert (
        plan_completion(
            replace(VIEW, controller=replace(VIEW.controller, completion_backlog=5)),
            replace(REQUEST, policy=replace(REQUEST.policy, backlog_limit=5)),
        ).status
        is CompletionStatus.BACKPRESSURE
    )
    port = Port()

    def fail(p: Port) -> None:
        raise AcquisitionFailure("missing pages")

    port.on_read = fail
    assert CompletionController(port).tick(REQUEST).status is CompletionStatus.ACQUISITION


def test_human_prerequisite_duplicate_and_existing_lease_holds() -> None:
    port = Port()
    mutate(port, "human")
    assert plan_completion(port.view, REQUEST).status is CompletionStatus.HUMAN
    c = VIEW.controller
    s = c.graph.snapshot
    prerequisite = WorkId(REQUEST.repository, "11")
    dependent = replace(
        s,
        works=(*s.works, Work(prerequisite, "Required")),
        dependencies=frozenset({(WORK, prerequisite)}),
    )
    assert (
        plan_completion(
            replace(VIEW, controller=replace(c, graph=replace(c.graph, snapshot=dependent))),
            REQUEST,
        ).status
        is CompletionStatus.PREREQUISITE
    )
    duplicate = replace(
        s, changes=(*s.changes, Change(ChangeId(REQUEST.repository, "16"), WORK, HEAD, PRODUCER))
    )
    assert (
        plan_completion(
            replace(VIEW, controller=replace(c, graph=replace(c.graph, snapshot=duplicate))),
            REQUEST,
        ).status
        is CompletionStatus.OWNERSHIP
    )
    for lease in [
        Lease(LeaseId("other"), WORK, PRODUCER, 1, 5),
        Lease(LeaseId("other"), WORK, PRODUCER, 1, 50),
    ]:
        assert (
            plan_completion(
                replace(
                    VIEW,
                    controller=replace(
                        c, graph=replace(c.graph, snapshot=replace(s, leases=(lease,)))
                    ),
                ),
                REQUEST,
            ).status
            is CompletionStatus.OWNERSHIP
        )


def test_command_identity_binds_evidence_request_and_ownership() -> None:
    ports = [Port() for _ in range(4)]
    ports[1].view = replace(
        VIEW,
        candidates=(
            replace(
                EVIDENCE, reviews=(replace(EVIDENCE.reviews[0], id=ReviewId("new-attestation")),)
            ),
        ),
    )
    requests = [
        REQUEST,
        REQUEST,
        replace(REQUEST, lease_ttl=21),
        replace(REQUEST, required_readiness=frozenset({"mechanical", "additional"})),
    ]
    ports[3].view = replace(
        VIEW,
        candidates=(
            replace(
                EVIDENCE,
                readiness=(
                    *EVIDENCE.readiness,
                    replace(
                        EVIDENCE.readiness[0],
                        id=replace(EVIDENCE.readiness[0].id, name="additional"),
                    ),
                ),
            ),
        ),
    )
    for p, r in zip(ports, requests, strict=True):
        assert CompletionController(p).tick(r, apply=True).status is CompletionStatus.COMPLETED
    assert len({p.commands[0].command.idempotency_key for p in ports}) == 4
    assert len({p.commands[1].command.idempotency_key for p in ports}) == 4


def test_one_ordered_handoff_never_skips_held_evidence() -> None:
    c = VIEW.controller
    s = c.graph.snapshot
    other_work = WorkId(REQUEST.repository, "13")
    other_change = ChangeId(REQUEST.repository, "16")
    e = replace(EVIDENCE, provenance=replace(EVIDENCE.provenance, change=other_change))
    port = Port()
    port.view = replace(
        VIEW,
        candidates=(EVIDENCE, e),
        controller=replace(
            c,
            graph=replace(
                c.graph,
                snapshot=replace(
                    s,
                    works=(*s.works, Work(other_work, "Other")),
                    changes=(*s.changes, Change(other_change, other_work, HEAD, PRODUCER)),
                ),
            ),
        ),
    )
    assert CompletionController(port).tick(REQUEST, apply=True).status is CompletionStatus.COMPLETED
    assert len(port.merged) == 1
    port.view = replace(
        port.view,
        candidates=(replace(EVIDENCE, provenance=replace(EVIDENCE.provenance, trusted=False)), e),
    )
    assert (
        plan_completion(port.view, replace(REQUEST, change=other_change)).status
        is CompletionStatus.PROVENANCE
    )


@pytest.mark.parametrize("boundary", [EffectKind.CLAIM, EffectKind.COMPLETE])
def test_adapter_clock_rechecks_expiry_and_age(boundary: EffectKind) -> None:
    port = Port()
    port.on_effect = lambda p, e: setattr(p, "now", 30) if e.command.kind is boundary else None
    assert CompletionController(port).tick(REQUEST, apply=True).status is CompletionStatus.STALE
    assert not port.merged


@pytest.mark.parametrize(
    "kind",
    ["contributors", "reviews", "ci", "readiness", "human", "worker", "renewal", "replacement"],
)
def test_enforcement_extends_through_actual_merge_transaction(kind: str) -> None:
    port = Port()
    port.before_merge = lambda p: mutate(p, kind)
    assert CompletionController(port).tick(REQUEST, apply=True).status is CompletionStatus.REFUSED
    assert not port.merged


def test_known_unrelated_activity_uses_capacity_and_new_activity_is_fenced() -> None:
    from rotisserie.domain import Worker, WorkerId

    other = WorkerId("agent", "unrelated")
    c = VIEW.controller
    s = replace(c.graph.snapshot, workers=(*c.graph.snapshot.workers, Worker(other)))
    v = replace(
        VIEW,
        controller=replace(
            c,
            active=1,
            liveness=(*c.liveness, (other, Liveness.ACTIVE)),
            graph=replace(c.graph, snapshot=s),
        ),
    )
    assert (
        plan_completion(v, replace(REQUEST, workers=REQUEST.workers | {other})).status
        is CompletionStatus.SELECTED
    )


def test_contradictory_graph_ci_does_not_override_trusted_failed_evidence() -> None:
    c = VIEW.controller
    s = replace(c.graph.snapshot, checks=EVIDENCE.ci)
    e = replace(EVIDENCE, ci=(replace(EVIDENCE.ci[0], status=CheckStatus.FAILED),))
    assert (
        plan_completion(
            replace(
                VIEW, controller=replace(c, graph=replace(c.graph, snapshot=s)), candidates=(e,)
            ),
            REQUEST,
        ).status
        is CompletionStatus.CI
    )


def test_conflicting_required_graph_check_holds_closed() -> None:
    c = VIEW.controller
    failed = replace(EVIDENCE.ci[0], status=CheckStatus.FAILED)
    s = replace(c.graph.snapshot, checks=(failed,))
    assert (
        plan_completion(
            replace(VIEW, controller=replace(c, graph=replace(c.graph, snapshot=s))), REQUEST
        ).status
        is CompletionStatus.CI
    )


@pytest.mark.parametrize("outcome", ["completed", {}, [], None])
def test_malformed_completion_acknowledgement_is_uncertain(outcome: Any) -> None:
    class MalformedPort(Port):
        def complete(self, e: CompletionEffect) -> Any:
            super().complete(e)
            return outcome

    port = MalformedPort()
    assert CompletionController(port).tick(REQUEST, apply=True).status is CompletionStatus.UNCERTAIN
    assert port.view.records and port.view.controller.graph.snapshot.leases


def test_missing_worker_observations_hold_closed() -> None:
    c = VIEW.controller
    absent = REQUEST.worker
    view = replace(
        VIEW,
        controller=replace(c, liveness=tuple((w, state) for w, state in c.liveness if w != absent)),
    )
    assert plan_completion(view, REQUEST).status is CompletionStatus.LIVENESS
    assert (
        plan_completion(replace(view, candidates=()), REQUEST).status is CompletionStatus.LIVENESS
    )
