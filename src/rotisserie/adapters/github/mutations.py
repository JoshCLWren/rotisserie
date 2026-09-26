"""Fail-closed, idempotent GitHub mutation execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from rotisserie.domain import RepositoryId


class MutationError(RuntimeError):
    """A mutation was refused or did not reconcile atomically."""


@dataclass(frozen=True, repr=False)
class SecretToken:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("credential cannot be empty")

    def __repr__(self) -> str:
        return "SecretToken(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


class MutationTarget(StrEnum):
    ISSUE = "issue"
    CHANGE = "change"


@dataclass(frozen=True)
class MutationScope:
    repository: RepositoryId
    installation_id: int
    issues: frozenset[int] = frozenset()
    changes: frozenset[int] = frozenset()

    def allows(self, target: MutationTarget, number: int) -> bool:
        allowed = self.issues if target is MutationTarget.ISSUE else self.changes
        return number in allowed


@dataclass(frozen=True)
class MutationPlan:
    repository: RepositoryId
    target: MutationTarget
    number: int
    labels: frozenset[str]
    state: str
    expected_head: str | None = None
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if self.number <= 0 or self.state not in {"open", "closed"}:
            raise ValueError("invalid mutation target state")
        if any(not label or label != label.strip() for label in self.labels):
            raise ValueError("labels must be non-empty trimmed strings")
        canonical = {
            "repository": [self.repository.host, self.repository.owner, self.repository.name],
            "target": self.target,
            "number": self.number,
            "labels": sorted(self.labels),
            "state": self.state,
            "expected_head": self.expected_head,
        }
        derived = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.idempotency_key and self.idempotency_key != derived:
            raise ValueError("idempotency key does not match mutation content")
        object.__setattr__(self, "idempotency_key", derived)


@dataclass(frozen=True)
class ReconciliationResult:
    labels: frozenset[str]
    state: str
    head: str | None = None


class CredentialProvider(Protocol):
    def token_for(self, repository: RepositoryId, installation_id: int) -> SecretToken | None: ...


class GitHubTransport(Protocol):
    def inspect(
        self, repository: RepositoryId, target: MutationTarget, number: int, token: SecretToken
    ) -> ReconciliationResult: ...

    def reconcile(self, plan: MutationPlan, token: SecretToken) -> ReconciliationResult: ...


class MarkerStore(Protocol):
    def contains(self, key: str) -> bool: ...

    def record(self, key: str) -> None: ...


@dataclass
class InMemoryMarkerStore:
    keys: set[str] = field(default_factory=set)

    def contains(self, key: str) -> bool:
        return key in self.keys

    def record(self, key: str) -> None:
        self.keys.add(key)


class GitHubMutationAdapter:
    """Execute complete label/state replacement within one configured scope."""

    def __init__(
        self,
        scope: MutationScope,
        credentials: CredentialProvider,
        transport: GitHubTransport,
        markers: MarkerStore,
        *,
        dry_run: bool = False,
    ) -> None:
        self._scope = scope
        self._credentials = credentials
        self._transport = transport
        self._markers = markers
        self._dry_run = dry_run
        self.plans: list[MutationPlan] = []

    def execute(self, plan: MutationPlan) -> ReconciliationResult | None:
        if plan.repository != self._scope.repository:
            raise MutationError("repository is outside the configured mutation scope")
        if not self._scope.allows(plan.target, plan.number):
            raise MutationError("target is not allowlisted for mutation")
        if plan.target is MutationTarget.CHANGE and plan.expected_head is None:
            raise MutationError("change mutations require an exact expected head")
        if self._markers.contains(plan.idempotency_key):
            return None
        self.plans.append(plan)
        if self._dry_run:
            return None
        token = self._credentials.token_for(self._scope.repository, self._scope.installation_id)
        if token is None:
            raise MutationError("credential selection failed closed")
        before = self._transport.inspect(plan.repository, plan.target, plan.number, token)
        if plan.expected_head is not None and before.head != plan.expected_head:
            raise MutationError("target head changed; refusing stale mutation")
        result = self._transport.reconcile(plan, token)
        if result.labels != plan.labels or result.state != plan.state:
            raise MutationError("GitHub did not atomically reconcile the requested state")
        if plan.expected_head is not None and result.head != plan.expected_head:
            raise MutationError("target head changed during reconciliation")
        self._markers.record(plan.idempotency_key)
        return result

    def public_plan_records(self) -> tuple[Mapping[str, object], ...]:
        """Return prompt/log-safe plan data; credentials never enter plans."""

        return tuple(
            {
                "repository": f"{plan.repository.owner}/{plan.repository.name}",
                "target": plan.target,
                "number": plan.number,
                "labels": sorted(plan.labels),
                "state": plan.state,
                "expected_head": plan.expected_head,
                "idempotency_key": plan.idempotency_key,
            }
            for plan in self.plans
        )
