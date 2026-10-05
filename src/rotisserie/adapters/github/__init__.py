"""Secure GitHub projection and mutation boundaries."""

from rotisserie.adapters.github.mutations import (
    CredentialProvider,
    GitHubMutationAdapter,
    GitHubTransport,
    InMemoryMarkerStore,
    MutationPlan,
    MutationScope,
    MutationTarget,
    ReconciliationResult,
    SecretToken,
)
from rotisserie.adapters.github.projection import GitHubProjector, ProjectionError
from rotisserie.adapters.github.review_evidence import (
    GitHubReviewEvidence,
    HeadContributorMarker,
    ReviewEvidenceError,
    TrustedCommentPolicy,
)

__all__ = [
    "CredentialProvider",
    "GitHubMutationAdapter",
    "GitHubProjector",
    "GitHubReviewEvidence",
    "GitHubTransport",
    "HeadContributorMarker",
    "InMemoryMarkerStore",
    "MutationPlan",
    "MutationScope",
    "MutationTarget",
    "ProjectionError",
    "ReconciliationResult",
    "ReviewEvidenceError",
    "SecretToken",
    "TrustedCommentPolicy",
]
