"""Portable review-independence rule over pre-decoded, provider-neutral signals.

This is the single canonical home for the deterministic rule that decides
whether the approvals attested against one exact revision satisfy independent
review. It ports the shared semantics ComicPile keeps in
``.github/scripts/factory_review_policy.py`` (``head_has_authorized_approval``
and the head-contributor provenance it consumes) into Rotisserie's domain, but
it owns only the *rule*: it never parses GitHub payloads, HTML markers,
pull-request bodies, branch names, or any other host- or product-specific
encoding.

An adopter's translator layer is responsible for decoding its own vocabulary
(head-contributor markers, trusted comment authorship, branch/body producer
prose, factory worker labels) into the structured
:class:`ReviewIndependenceSignals` consumed here. The rule then evaluates
identically for every adopter.

The rule generalizes :func:`rotisserie.domain.policy.reviewer_is_independent`
from a single producer identity to the complete *exact-head contributor set*:
every worker that authored the revision under review, unioned with the whole
recorded lineage of the change (because a repair stacks commits onto the prior
head rather than rewriting it) and the declared producer. A reviewer is
eligible only when absent from that set. This is a worker-identity boundary,
not a model or provider boundary: two workers routed through the same upstream
model are still distinct contributors.

The evaluation is pure, deterministic, and fails closed. When a trusted record
exists for the exact revision (``provenance_recorded``), one eligible reviewer
authorizes it. When no such record exists, the evidence is missing or ambiguous
and the rule refuses to assume the apparent reviewer never touched the head: it
requires two distinct eligible reviewers instead.

This rule is additive: :func:`rotisserie.domain.policy.readiness_decision` is
not yet wired to it and still consumes the legacy single-producer
:func:`rotisserie.domain.policy.reviewer_is_independent`. Callers that need
full exact-head contributor-set independence should evaluate this rule
directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rotisserie.domain.model import RevisionId, WorkerId

SINGLE_REVIEWER = 1
DUAL_REVIEWER = 2


class ReviewIndependenceBlock(StrEnum):
    """A stable reason why exact-head approvals do not satisfy independent review.

    ``NO_ELIGIBLE_REVIEWER`` means no approver is independent of the exact-head
    contributor set. ``INDEPENDENT_REVIEW_UNRECORDED`` means provenance for this
    exact head is not recorded, so the fail-closed dual-review requirement was
    not met. ``INDEPENDENT_REVIEW_PRODUCER_UNKNOWN`` means provenance *is*
    recorded but the producer is unknown or unattested, so single-review
    authorization is withheld and the fail-closed dual-review requirement was not
    met. The last two are distinct causes and are never emitted interchangeably.
    """

    NO_ELIGIBLE_REVIEWER = "no_eligible_reviewer"
    INDEPENDENT_REVIEW_UNRECORDED = "independent_review_unrecorded"
    INDEPENDENT_REVIEW_PRODUCER_UNKNOWN = "independent_review_producer_unknown"


@dataclass(frozen=True)
class ReviewIndependenceSignals:
    """Decoded, host-neutral review-independence inputs for one exact revision.

    Every field is already translated out of its host or product encoding by an
    adopter translator. The domain never reconstructs marker text, comment
    authorship, or branch prose from these values.

    Attributes:
        revision: The exact revision whose approvals are under review. Review,
            CI, and readiness evidence belongs to this precise head; a changed
            head invalidates it.
        contributors: Every worker that authored the lineage reachable from this
            head. The adopter unions the workers recorded against the exact head
            with the whole-change recorded lineage, because repairs stack onto
            the previous head instead of rewriting it. Any worker here is barred
            from reviewing the head.
        approvers: The distinct workers who attested an approval for this exact
            revision. Approvals for any other head do not belong here.
        producer: The producer identity to exclude from review, or ``None`` when
            no producer is known. This value is always excluded from the eligible
            reviewer set regardless of its source, but on its own it never grants
            single-reviewer authorization; see ``producer_attested``.
        producer_attested: Whether ``producer`` is backed by exact-head
            attestation evidence (not merely a caller-supplied cross-check).
            Single-reviewer authorization requires this to be ``True`` so a
            producer value a worker could influence cannot downgrade the
            fail-closed dual-review requirement.
        provenance_recorded: Whether a trusted contributor record exists for this
            exact revision. A single eligible reviewer authorizes the head only
            when provenance is recorded *and* the producer is attested; otherwise
            the record is missing or ambiguous and review fails closed to two
            distinct eligible reviewers.
    """

    revision: RevisionId
    contributors: frozenset[WorkerId] = frozenset()
    approvers: frozenset[WorkerId] = frozenset()
    producer: WorkerId | None = None
    producer_attested: bool = False
    provenance_recorded: bool = False

    def excluded_reviewers(self) -> frozenset[WorkerId]:
        """Return every worker barred from reviewing this head.

        The exact-head contributor set is the recorded contributors unioned with
        the producer. A reviewer drawn from this set is never independent of the
        head.
        """

        excluded = set(self.contributors)
        if self.producer is not None:
            excluded.add(self.producer)
        return frozenset(excluded)

    def eligible_reviewers(self) -> frozenset[WorkerId]:
        """Return the approvers who are independent of the exact-head contributors."""

        return frozenset(self.approvers - self.excluded_reviewers())


@dataclass(frozen=True)
class ReviewIndependenceVerdict:
    """One revision's independent-review outcome with stable machine reasons.

    ``satisfied`` is true only when at least ``required_reviewers`` distinct
    eligible reviewers approved the exact head. ``exclusions`` carries the stable
    :class:`ReviewIndependenceBlock` codes in canonical order with parallel
    human-readable ``reasons``.
    """

    revision: RevisionId
    satisfied: bool
    eligible_reviewers: frozenset[WorkerId]
    required_reviewers: int
    exclusions: tuple[ReviewIndependenceBlock, ...]
    reasons: tuple[str, ...]


def evaluate_review_independence(
    signals: ReviewIndependenceSignals,
) -> ReviewIndependenceVerdict:
    """Decide whether exact-head approvals satisfy worker-independent review.

    The rule fails closed: a reviewer is eligible only when absent from the
    exact-head contributor set. A single eligible reviewer authorizes the head
    only when provenance is recorded *and* the producer is attested by exact-head
    evidence; when the producer is unknown or merely cross-checked, or provenance
    is unrecorded, two distinct eligible reviewers are required so an unexcluded
    or caller-influenced producer cannot self-authorize its own head. The
    unrecorded-provenance and unattested-producer causes carry distinct stable
    codes. Reasons are emitted in a fixed order so verdicts are deterministic for
    identical signals.
    """

    eligible = signals.eligible_reviewers()
    single_reviewer_authorized = signals.provenance_recorded and signals.producer_attested
    required = SINGLE_REVIEWER if single_reviewer_authorized else DUAL_REVIEWER
    exclusions: list[ReviewIndependenceBlock] = []
    reasons: list[str] = []

    if not eligible:
        exclusions.append(ReviewIndependenceBlock.NO_ELIGIBLE_REVIEWER)
        reasons.append("no approver is independent of the exact-head contributor set")
    elif len(eligible) < required:
        if not signals.provenance_recorded:
            code = ReviewIndependenceBlock.INDEPENDENT_REVIEW_UNRECORDED
            detail = "exact-head provenance is unrecorded"
        else:
            code = ReviewIndependenceBlock.INDEPENDENT_REVIEW_PRODUCER_UNKNOWN
            detail = "the exact-head producer is unattested"
        exclusions.append(code)
        reasons.append(
            f"{detail}: two distinct independent reviewers are required but only "
            f"{len(eligible)} of the approvers are independent"
        )

    satisfied = not exclusions
    if satisfied:
        reasons.append(
            "independent review satisfied: "
            f"{len(eligible)} eligible reviewer(s) meet the required {required}"
        )
    return ReviewIndependenceVerdict(
        signals.revision,
        satisfied,
        eligible,
        required,
        tuple(exclusions),
        tuple(reasons),
    )
