"""Read-only collection of exact-head review-independence evidence from GitHub.

This adapter translates GitHub review-thread payloads into the provider-neutral
:class:`~rotisserie.domain.review.ReviewIndependenceSignals` consumed by the
domain rule. It performs no mutations: it only reads comment bodies and derives
the exact-head contributor set, the recorded lineage, the approver set, and
whether provenance was recorded.

Marker vocabulary is an adopter concern, so the collector is configured with a
``namespace`` (the HTML-comment marker prefix, for example ComicPile's
``comic-pile-factory``) and a ``worker_namespace`` for the identities the
markers name. Rotisserie's domain never sees those strings; the GitHub- and
product-specific encoding is confined to this adapter.

Two anti-forgery rules from the source policy are preserved and applied to
*every* marker family, contributor records and semantic-review markers alike:

* Only comments authored by trusted actors (the controller bot login, a trusted
  association, and never an untrusted worker GitHub App) are read for
  provenance; see :class:`TrustedCommentPolicy`. The trusted-author set is empty
  by default and fails closed: an adopter must opt its controller identity in.
* A marker is honored only when the entire comment body is exactly that one
  marker, so a worker cannot forge provenance or an approval by pasting a marker
  into a resume packet, review prose, or a commit message.

Producer identity is taken from the producer attested in the exact-head review
markers, not from the optional caller parameter. A declared producer that
contradicts the attested one, or contradictory attestations for the same head,
fail closed. This prevents a producer with no recorded contribution from
appearing as its own eligible reviewer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from rotisserie.domain import (
    RepositoryId,
    ReviewIndependenceSignals,
    RevisionId,
    WorkerId,
)

_NAMESPACE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOKEN = r"[^:\s>]+"
_DEFAULT_VERDICTS = frozenset(
    {"approve", "repair", "reject", "obsolete", "duplicate", "superseded", "delivered"}
)


class ReviewEvidenceError(ValueError):
    """A collector was configured or fed with ambiguous or unsafe evidence."""


@dataclass(frozen=True)
class TrustedCommentPolicy:
    """Which comment authors may be read for trusted review provenance.

    **The default fails closed: it trusts no one.** A shared default
    ``GITHUB_TOKEN`` Actions job posts as ``github-actions[bot]``, so trusting
    that login (or the ``github-actions`` App slug) out of the box would let any
    worker running in Actions forge controller provenance. An adopter must
    therefore opt its controller identity in explicitly: set ``bot_login`` to a
    dedicated controller login and/or ``trusted_associations`` for a human
    operator running the controller during an incident, and list only the App
    slugs the controller legitimately posts through. Worker GitHub Apps must
    never be added; an App slug outside ``trusted_app_slugs`` loses even when the
    association looks trusted.
    """

    bot_login: str = ""
    trusted_associations: frozenset[str] = frozenset()
    trusted_app_slugs: frozenset[str] = frozenset()

    def _performed_via_untrusted_app(self, comment: Mapping[str, Any]) -> bool:
        app = comment.get("performed_via_github_app")
        if not isinstance(app, Mapping):
            return False
        slug = app.get("slug")
        if not isinstance(slug, str) or not slug:
            return False
        return slug not in self.trusted_app_slugs

    def trusts(self, comment: Mapping[str, Any]) -> bool:
        """Return whether one comment was authored by a trusted actor.

        A positive trust signal is required: an empty ``bot_login`` never matches
        a login and an empty ``trusted_associations`` never matches an
        association, so the default configuration trusts nothing.
        """

        if self._performed_via_untrusted_app(comment):
            return False
        user = comment.get("user")
        if not isinstance(user, Mapping):
            return False
        login = str(user.get("login") or "")
        association = str(comment.get("author_association") or "")
        if self.bot_login and login == self.bot_login:
            return True
        return bool(association) and association in self.trusted_associations


@dataclass(frozen=True)
class HeadContributorMarker:
    """One parsed, controller-authored head-contributor record."""

    pr: int
    head: str
    worker: str
    epoch: int


@dataclass(frozen=True)
class _ReviewScan:
    """The four evidence sets derived from one single pass over trusted bodies."""

    lineage: frozenset[str]
    exact_contributors: frozenset[str]
    approvers: frozenset[str]
    attested_producers: frozenset[str]


class GitHubReviewEvidence:
    """Project GitHub review threads into exact-head review-independence signals.

    The collector is read-only and bound to one repository, one marker
    ``namespace``, and one ``worker_namespace``. Payload text can neither
    retarget the repository nor mint identities outside the configured
    namespace.
    """

    def __init__(
        self,
        repository: RepositoryId,
        *,
        namespace: str,
        worker_namespace: str,
        verdicts: frozenset[str] = _DEFAULT_VERDICTS,
        policy: TrustedCommentPolicy | None = None,
    ) -> None:
        if not _NAMESPACE_RE.fullmatch(namespace):
            raise ReviewEvidenceError("marker namespace must be a hyphenated lowercase token")
        if not worker_namespace or worker_namespace != worker_namespace.strip():
            raise ReviewEvidenceError("worker namespace must be a non-empty, trimmed string")
        if "approve" not in verdicts:
            raise ReviewEvidenceError("verdict vocabulary must include 'approve'")
        self.repository = repository
        self.namespace = namespace
        self.worker_namespace = worker_namespace
        self._policy = policy or TrustedCommentPolicy()
        escaped = re.escape(namespace)
        verdict_alternation = "|".join(sorted(re.escape(verdict) for verdict in verdicts))
        self._contributor_re = re.compile(
            rf"^<!-- {escaped}-head-contributor-v1:"
            rf"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}):"
            rf"worker-(?P<worker>{_TOKEN}):epoch-(?P<epoch>\d+) -->$"
        )
        self._review_re = re.compile(
            rf"^<!-- {escaped}-semantic-review-v1:"
            rf"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}):"
            rf"reviewer-(?P<reviewer>{_TOKEN}):producer-(?P<producer>{_TOKEN}):"
            rf"verdict-(?P<verdict>{verdict_alternation}) -->$"
        )

    def parse_head_contributor_marker(self, line: str) -> HeadContributorMarker | None:
        """Parse one exact head-contributor marker line, or return ``None``."""

        match = self._contributor_re.fullmatch(line.strip())
        if match is None:
            return None
        return HeadContributorMarker(
            pr=int(match["pr"]),
            head=match["head"],
            worker=match["worker"],
            epoch=int(match["epoch"]),
        )

    def trusted_bodies(self, comments: Iterable[Mapping[str, Any]]) -> list[str]:
        """Return the bodies of comments authored by trusted actors.

        Every consumer of review provenance must apply this identical filter; if
        the controller, the capture, and the dispatcher disagree about who
        authored a marker they can disagree about who authored a head.
        """

        bodies: list[str] = []
        for comment in comments:
            if not isinstance(comment, Mapping):
                raise ReviewEvidenceError("each comment must be an object")
            if not self._policy.trusts(comment):
                continue
            bodies.append(str(comment.get("body") or ""))
        return bodies

    def _scan(self, bodies: Iterable[str], *, pr: int, head: str) -> _ReviewScan:
        """Derive every evidence set in one guarded pass over the trusted bodies.

        A single whole-body forgery guard protects both marker families, so the
        contributor and approval anti-forgery rules cannot drift apart, and each
        body is matched against each marker regex at most once.
        """

        lineage: set[str] = set()
        exact_contributors: set[str] = set()
        approvers: set[str] = set()
        attested_producers: set[str] = set()
        for body in bodies:
            stripped = body.strip()
            if not stripped or "\n" in stripped:
                continue
            contributor = self._contributor_re.fullmatch(stripped)
            if contributor is not None:
                if int(contributor["pr"]) != pr:
                    continue
                lineage.add(contributor["worker"])
                if contributor["head"] == head:
                    exact_contributors.add(contributor["worker"])
                continue
            review = self._review_re.fullmatch(stripped)
            if review is None or int(review["pr"]) != pr or review["head"] != head:
                continue
            if review["producer"] != "unknown":
                attested_producers.add(review["producer"])
            if review["verdict"] == "approve":
                approvers.add(review["reviewer"])
        return _ReviewScan(
            frozenset(lineage),
            frozenset(exact_contributors),
            frozenset(approvers),
            frozenset(attested_producers),
        )

    def _worker(self, token: str) -> WorkerId:
        return WorkerId(self.worker_namespace, token)

    def collect(
        self,
        comments: Iterable[Mapping[str, Any]],
        *,
        pr: int,
        head: str,
        producer: str | None = None,
    ) -> ReviewIndependenceSignals:
        """Build exact-head review-independence signals from a PR's comments.

        ``head`` is the exact 40-character revision under review. The returned
        signals carry the recorded lineage contributors, the exact-head
        approvers, the authoritative producer, and whether a trusted record
        exists for this exact head. The whole collection is read-only.

        The producer is taken from the identity attested in the exact-head review
        markers rather than from the ``producer`` parameter. The parameter is an
        optional cross-check: when supplied it must agree with the attestation or
        the collection fails closed. Contradictory attestations for one head also
        fail closed. This prevents a producer with no recorded contribution from
        slipping through as its own eligible reviewer.
        """

        if pr <= 0:
            raise ReviewEvidenceError("pull request number must be positive")
        head = head.strip().lower()
        if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
            raise ReviewEvidenceError("head must be a 40-character hexadecimal revision")
        declared = producer.strip() if producer is not None else None
        if declared == "":
            raise ReviewEvidenceError("producer, when provided, must be a non-empty token")
        bodies = self.trusted_bodies(comments)
        scan = self._scan(bodies, pr=pr, head=head)
        if len(scan.attested_producers) > 1:
            raise ReviewEvidenceError("contradictory producer attestation for the exact head")
        attested = next(iter(scan.attested_producers), None)
        if attested is not None and declared is not None and declared != attested:
            raise ReviewEvidenceError(
                "declared producer contradicts the attested exact-head producer"
            )
        authoritative = attested if attested is not None else declared
        return ReviewIndependenceSignals(
            revision=RevisionId(self.repository, head),
            contributors=frozenset(self._worker(worker) for worker in scan.lineage),
            approvers=frozenset(self._worker(worker) for worker in scan.approvers),
            producer=self._worker(authoritative) if authoritative else None,
            provenance_recorded=bool(scan.exact_contributors),
        )
