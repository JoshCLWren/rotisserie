from dataclasses import replace

import pytest

from rotisserie.application.controller import ControllerView, Liveness
from rotisserie.application.coordination import EffectConflict, EffectFailure, GraphView
from rotisserie.application.recovery import (
    ActivityEvidence,
    LeaseRecoveryController,
    RecoveryRelease,
    RecoveryRequest,
    RecoveryStatus,
    RecoveryView,
    plan_recovery,
)
from rotisserie.domain import (
    GraphSnapshot,
    Lease,
    LeaseId,
    RepositoryId,
    Work,
    Worker,
    WorkerId,
    WorkId,
)

REPO = RepositoryId("example.test", "org", "repo")
WORKER = WorkerId("agent", "one")
WORK = WorkId(REPO, "1")
LEASE = Lease(LeaseId("lease"), WORK, WORKER, 1, 5)
REQUEST = RecoveryRequest(REPO, "recover-1", 20, 0, 10, frozenset({WORKER}))
VIEW = RecoveryView(
    ControllerView(
        GraphView(
            GraphSnapshot(
                works=(Work(WORK, "work"),),
                workers=(Worker(WORKER),),
                leases=(LEASE,),
            ),
            "1",
        ),
        20,
        1,
        0,
        ((WORKER, Liveness.IDLE),),
    ),
    (ActivityEvidence(LEASE, 20, 2, True),),
    True,
)


class Port:
    def __init__(self) -> None:
        self.view = VIEW
        self.next_view = VIEW
        self.reads = 0
        self.releases: list[RecoveryRelease] = []
        self.error: Exception | None = None
        self.race: RecoveryView | None = None

    def acquire(self) -> RecoveryView:
        self.reads += 1
        return self.view if self.reads == 1 else self.next_view

    def release(self, release: RecoveryRelease) -> None:
        live = self.race or self.next_view
        if release.command.expected_version != live.controller.graph.version:
            raise EffectConflict("changed observation")
        if release.expected_lease not in live.controller.graph.snapshot.leases:
            raise EffectConflict("changed lease")
        if plan_recovery(live, release.request).status is not RecoveryStatus.PLANNED:
            raise EffectConflict("new activity")
        if self.error:
            raise self.error
        self.releases.append(release)
        self.next_view = replace(
            live,
            controller=replace(
                live.controller,
                graph=GraphView(
                    replace(
                        live.controller.graph.snapshot,
                        leases=tuple(
                            x
                            for x in live.controller.graph.snapshot.leases
                            if x != release.expected_lease
                        ),
                    ),
                    "2",
                ),
            ),
        )


def test_bounded_release_and_dry_run() -> None:
    port = Port()
    controller = LeaseRecoveryController(port)
    assert controller.recover(REQUEST).status is RecoveryStatus.PLANNED
    assert port.reads == 1 and not port.releases
    port.reads = 0
    result = controller.recover(REQUEST, apply=True)
    assert result.status is RecoveryStatus.RELEASED
    assert port.reads == 2 and len(port.releases) == 1
    assert result.to_dict()["schema_version"] == 1
    assert result.commands == (port.releases[0].command.idempotency_key,)
    assert controller.recover(REQUEST, apply=True).status is RecoveryStatus.ZERO


@pytest.mark.parametrize(
    "view,status",
    [
        (replace(VIEW, activity=()), RecoveryStatus.OBSERVATION_FAILED),
        (
            replace(VIEW, activity=(replace(VIEW.activity[0], trusted=False),)),
            RecoveryStatus.OBSERVATION_FAILED,
        ),
        (
            replace(VIEW, activity=(replace(VIEW.activity[0], latest_activity_at=None),)),
            RecoveryStatus.OBSERVATION_FAILED,
        ),
        (
            replace(VIEW, activity=(replace(VIEW.activity[0], observed_at=19),)),
            RecoveryStatus.OBSERVATION_FAILED,
        ),
        (
            replace(VIEW, activity=(replace(VIEW.activity[0], latest_activity_at=21),)),
            RecoveryStatus.OBSERVATION_FAILED,
        ),
        (
            replace(VIEW, activity=(replace(VIEW.activity[0], latest_activity_at=0),)),
            RecoveryStatus.OBSERVATION_FAILED,
        ),
        (
            replace(VIEW, activity=(replace(VIEW.activity[0], latest_activity_at=10),)),
            RecoveryStatus.ACTIVITY_HOLD,
        ),
        (replace(VIEW, liveness_complete=False), RecoveryStatus.LIVENESS_HOLD),
        (
            replace(VIEW, controller=replace(VIEW.controller, liveness=())),
            RecoveryStatus.LIVENESS_HOLD,
        ),
        (
            replace(
                VIEW, controller=replace(VIEW.controller, liveness=((WORKER, Liveness.ACTIVE),))
            ),
            RecoveryStatus.LIVENESS_HOLD,
        ),
        (
            replace(VIEW, controller=replace(VIEW.controller, observed_at=19)),
            RecoveryStatus.OBSERVATION_FAILED,
        ),
        (
            replace(VIEW, controller=replace(VIEW.controller, observed_at=21)),
            RecoveryStatus.OBSERVATION_FAILED,
        ),
    ],
)
def test_refusals_on_both_observations(view: RecoveryView, status: RecoveryStatus) -> None:
    for initial in (True, False):
        port = Port()
        if initial:
            port.view = view
        else:
            port.next_view = view
        assert LeaseRecoveryController(port).recover(REQUEST, apply=True).status is status
        assert not port.releases


@pytest.mark.parametrize(
    "lease",
    [
        replace(LEASE, expires_at=30),
        replace(LEASE, id=LeaseId("replacement")),
        replace(LEASE, worker=WorkerId("agent", "other")),
    ],
)
def test_reacquired_renewal_and_replacement_hold(lease: Lease) -> None:
    port = Port()
    snapshot = replace(
        VIEW.controller.graph.snapshot,
        workers=(Worker(WORKER), Worker(WorkerId("agent", "other"))),
        leases=(lease,),
    )
    port.next_view = replace(
        VIEW, controller=replace(VIEW.controller, graph=GraphView(snapshot, "2"))
    )
    assert (
        LeaseRecoveryController(port).recover(REQUEST, apply=True).status
        is not RecoveryStatus.RELEASED
    )
    assert not port.releases


@pytest.mark.parametrize("change", ["renewal", "replacement", "active", "activity"])
def test_mutation_boundary_races(change: str) -> None:
    port = Port()
    live = VIEW
    if change in {"renewal", "replacement"}:
        lease = (
            replace(LEASE, expires_at=30)
            if change == "renewal"
            else replace(LEASE, id=LeaseId("new"))
        )
        live = replace(
            live,
            controller=replace(
                live.controller,
                graph=GraphView(replace(live.controller.graph.snapshot, leases=(lease,)), "2"),
            ),
        )
    elif change == "active":
        live = replace(
            live, controller=replace(live.controller, liveness=((WORKER, Liveness.ACTIVE),))
        )
    else:
        live = replace(live, activity=(replace(live.activity[0], latest_activity_at=20),))
    port.race = live
    assert LeaseRecoveryController(port).recover(REQUEST, apply=True).status is RecoveryStatus.STALE
    assert not port.releases


@pytest.mark.parametrize(
    "error,status",
    [
        (EffectFailure("refused"), RecoveryStatus.FAILED),
        (ConnectionError("lost ack"), RecoveryStatus.UNCERTAIN),
    ],
)
def test_write_failure_categories(error: Exception, status: RecoveryStatus) -> None:
    port = Port()
    port.error = error
    assert LeaseRecoveryController(port).recover(REQUEST, apply=True).status is status
    assert port.next_view.controller.graph.snapshot.leases == (LEASE,)


def test_acquisition_exception_is_never_zero() -> None:
    class FailedPort(Port):
        def acquire(self) -> RecoveryView:
            raise ConnectionError("unavailable")

    assert (
        LeaseRecoveryController(FailedPort()).recover(REQUEST).status
        is RecoveryStatus.OBSERVATION_FAILED
    )


def test_unexpired_is_zero_and_duplicate_evidence_is_invalid() -> None:
    assert (
        plan_recovery(VIEW, replace(REQUEST, at=4, max_observation_age=20)).status
        is RecoveryStatus.OBSERVATION_FAILED
    )
    view = replace(VIEW, controller=replace(VIEW.controller, observed_at=4))
    assert plan_recovery(view, replace(REQUEST, at=4)).status is RecoveryStatus.ZERO
    with pytest.raises(ValueError):
        replace(VIEW, activity=(*VIEW.activity, *VIEW.activity))


def test_one_release_per_call_and_never_skip_a_held_first_lease() -> None:
    other_work = WorkId(REPO, "2")
    other = replace(LEASE, id=LeaseId("second"), work=other_work, expires_at=6)
    snapshot = replace(
        VIEW.controller.graph.snapshot,
        works=(*VIEW.controller.graph.snapshot.works, Work(other_work, "other")),
        leases=(other, LEASE),
    )
    view = replace(
        VIEW,
        controller=replace(VIEW.controller, graph=GraphView(snapshot, "1")),
        activity=(*VIEW.activity, ActivityEvidence(other, 20, 2, True)),
    )
    port = Port()
    port.view = port.next_view = view
    assert LeaseRecoveryController(port).recover(REQUEST, apply=True).lease == LEASE
    assert len(port.releases) == 1
    assert port.next_view.controller.graph.snapshot.leases == (other,)
    assert (
        plan_recovery(replace(view, activity=(view.activity[1],)), REQUEST).status
        is RecoveryStatus.OBSERVATION_FAILED
    )


def test_scope_and_unconfigured_owner_hold_closed() -> None:
    foreign = RepositoryId("example.test", "other", "repo")
    assert (
        plan_recovery(VIEW, replace(REQUEST, repository=foreign)).status
        is RecoveryStatus.OBSERVATION_FAILED
    )
    assert (
        plan_recovery(VIEW, replace(REQUEST, workers=frozenset())).status
        is RecoveryStatus.LIVENESS_HOLD
    )


def test_second_acquisition_failure_retains_lease() -> None:
    class FailedPort(Port):
        def acquire(self) -> RecoveryView:
            if self.reads:
                raise ConnectionError("second acquisition failed")
            return super().acquire()

    port = FailedPort()
    assert (
        LeaseRecoveryController(port).recover(REQUEST, apply=True).status
        is RecoveryStatus.OBSERVATION_FAILED
    )
    assert not port.releases


def test_lost_release_ack_does_not_trigger_another_effect() -> None:
    class LostAckPort(Port):
        def release(self, release: RecoveryRelease) -> None:
            super().release(release)
            raise ConnectionError("lost acknowledgement after durable release")

    port = LostAckPort()
    controller = LeaseRecoveryController(port)
    assert controller.recover(REQUEST, apply=True).status is RecoveryStatus.UNCERTAIN
    assert controller.recover(REQUEST, apply=True).status is RecoveryStatus.ZERO
    assert len(port.releases) == 1


def test_renewed_expired_lease_has_a_distinct_idempotency_key() -> None:
    first = Port()
    LeaseRecoveryController(first).recover(REQUEST, apply=True)
    renewed = replace(LEASE, expires_at=6)
    second = Port()
    second.view = second.next_view = replace(
        VIEW,
        controller=replace(
            VIEW.controller,
            graph=GraphView(replace(VIEW.controller.graph.snapshot, leases=(renewed,)), "2"),
        ),
        activity=(replace(VIEW.activity[0], lease=renewed),),
    )
    assert (
        LeaseRecoveryController(second).recover(REQUEST, apply=True).status
        is RecoveryStatus.RELEASED
    )
    assert first.releases[0].command.idempotency_key != second.releases[0].command.idempotency_key
