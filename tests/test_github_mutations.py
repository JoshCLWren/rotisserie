"""Security contracts for bounded GitHub write-side reconciliation."""

from dataclasses import dataclass, field, replace

import pytest

from rotisserie.adapters.github import (
    GitHubMutationAdapter,
    InMemoryMarkerStore,
    MutationPlan,
    MutationScope,
    MutationTarget,
    ReconciliationResult,
    SecretToken,
)
from rotisserie.adapters.github.mutations import MutationError
from rotisserie.domain import RepositoryId

REPOSITORY = RepositoryId("github.com", "acme", "oven")
OTHER = RepositoryId("github.com", "other", "oven")
HEAD = "a" * 40


@dataclass
class Credentials:
    token: SecretToken | None
    requested: list[tuple[RepositoryId, int]] = field(default_factory=list)

    def token_for(self, repository: RepositoryId, installation_id: int) -> SecretToken | None:
        self.requested.append((repository, installation_id))
        return self.token


@dataclass
class Transport:
    before: ReconciliationResult
    after: ReconciliationResult
    inspections: int = 0
    writes: int = 0

    def inspect(
        self,
        repository: RepositoryId,
        target: MutationTarget,
        number: int,
        token: SecretToken,
    ) -> ReconciliationResult:
        del repository, target, number, token
        self.inspections += 1
        return self.before

    def reconcile(self, plan: MutationPlan, token: SecretToken) -> ReconciliationResult:
        del plan, token
        self.writes += 1
        return self.after


def plan(repository: RepositoryId = REPOSITORY) -> MutationPlan:
    return MutationPlan(
        repository,
        MutationTarget.CHANGE,
        51,
        frozenset({"ready", "reviewed"}),
        "open",
        HEAD,
    )


def adapter(
    *, token: SecretToken | None = SecretToken("super-secret"), dry_run: bool = False
) -> tuple[GitHubMutationAdapter, Transport, InMemoryMarkerStore, Credentials]:
    result = ReconciliationResult(frozenset({"ready", "reviewed"}), "open", HEAD)
    transport = Transport(result, result)
    markers = InMemoryMarkerStore()
    credentials = Credentials(token)
    value = GitHubMutationAdapter(
        MutationScope(REPOSITORY, 123, changes=frozenset({51})),
        credentials,
        transport,
        markers,
        dry_run=dry_run,
    )
    return value, transport, markers, credentials


def test_exact_head_atomic_reconciliation_and_retry_marker() -> None:
    value, transport, markers, credentials = adapter()
    mutation = plan()
    assert value.execute(mutation) is not None
    assert credentials.requested == [(REPOSITORY, 123)]
    assert transport.writes == 1
    assert markers.contains(mutation.idempotency_key)
    assert value.execute(mutation) is None
    assert transport.writes == 1


def test_repository_target_credentials_and_stale_heads_fail_closed() -> None:
    value, transport, _, _ = adapter()
    with pytest.raises(MutationError, match="repository"):
        value.execute(plan(OTHER))
    with pytest.raises(MutationError, match="allowlisted"):
        value.execute(
            MutationPlan(
                REPOSITORY,
                MutationTarget.CHANGE,
                52,
                frozenset({"ready", "reviewed"}),
                "open",
                HEAD,
            )
        )

    missing, _, _, _ = adapter(token=None)
    with pytest.raises(MutationError, match="credential"):
        missing.execute(plan())

    stale, stale_transport, _, _ = adapter()
    stale_transport.before = replace(stale_transport.before, head="b" * 40)
    with pytest.raises(MutationError, match="head changed"):
        stale.execute(plan())
    assert stale_transport.writes == 0


def test_partial_label_result_is_rejected_without_durable_marker() -> None:
    value, transport, markers, _ = adapter()
    transport.after = replace(transport.after, labels=frozenset({"ready"}))
    mutation = plan()
    with pytest.raises(MutationError, match="atomically"):
        value.execute(mutation)
    assert not markers.contains(mutation.idempotency_key)


def test_dry_run_records_safe_plan_and_performs_no_remote_operations() -> None:
    value, transport, markers, credentials = adapter(dry_run=True)
    mutation = plan()
    assert value.execute(mutation) is None
    assert transport.inspections == transport.writes == 0
    assert credentials.requested == []
    assert not markers.contains(mutation.idempotency_key)
    rendered = repr(value.public_plan_records())
    assert "super-secret" not in rendered
    assert "ready" in rendered


def test_secret_repr_and_string_are_always_redacted() -> None:
    secret = SecretToken("never-print-this")
    assert "never-print-this" not in repr(secret)
    assert "never-print-this" not in str(secret)
