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

Two anti-forgery rules from the source policy are preserved:

* Only comments authored by trusted actors (the controller bot login, a trusted
  association, and never an untrusted worker GitHub App) are read for
  provenance; see :class:`TrustedCommentPolicy`.
* A head-contributor record is honored only when the entire comment body is one
  marker, so a worker cannot forge provenance by pasting a marker into a resume
  packet, review prose, or a commit message.
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

    The controller posts markers through ``GITHUB_TOKEN`` as the bot login, and
    a repository owner/member/collaborator may run the controller during an
    incident, so their comments are honored. A worker GitHub App is never one of
    those actors: a non-trusted App slug is ignored even when the association
    looks trusted. The specific worker App slugs are a product concern, so the
    trusted slug set is configurable and defaults to the GitHub Actions app.
    """

    bot_login: str = "github-actions[bot]"
    trusted_associations: frozenset[str] = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
    trusted_app_slugs: frozenset[str] = frozenset({"github-actions"})

    def _performed_via_untrusted_app(self, comment: Mapping[str, Any]) -> bool:
        app = comment.get("performed_via_github_app")
        if not isinstance(app, Mapping):
            return False
        slug = app.get("slug")
        if not isinstance(slug, str) or not slug:
            return False
        return slug not in self.trusted_app_slugs

    def trusts(self, comment: Mapping[str, Any]) -> bool:
        """Return whether one comment was authored by a trusted actor."""

        if self._performed_via_untrusted_app(comment):
            return False
        user = comment.get("user")
        if not isinstance(user, Mapping):
            return False
        login = str(user.get("login") or "")
        association = str(comment.get("author_association") or "")
        return login == self.bot_login or association in self.trusted_associations


@dataclass(frozen=True)
class HeadContributorMarker:
    """One parsed, controller-authored head-contributor record."""

    pr: int
    head: str
    worker: str
    epoch: int


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

    def _recorded_contributors(self, bodies: Iterable[str], *, pr: int) -> set[str]:
        """Return every worker with a whole-body contributor record on one PR."""

        workers: set[str] = set()
        for body in bodies:
            stripped = str(body or "").strip()
            if not stripped or "\n" in stripped:
                continue
            marker = self.parse_head_contributor_marker(stripped)
            if marker is None or marker.pr != pr:
                continue
            workers.add(marker.worker)
        return workers

    def _exact_head_contributors(self, bodies: Iterable[str], *, pr: int, head: str) -> set[str]:
        """Return workers whose whole-body record names this exact PR head."""

        workers: set[str] = set()
        for body in bodies:
            stripped = str(body or "").strip()
            if not stripped or "\n" in stripped:
                continue
            marker = self.parse_head_contributor_marker(stripped)
            if marker is None or marker.pr != pr or marker.head != head:
                continue
            workers.add(marker.worker)
        return workers

    def _exact_head_approvers(self, bodies: Iterable[str], *, pr: int, head: str) -> set[str]:
        """Return reviewers whose first-line approve marker names this exact head."""

        reviewers: set[str] = set()
        for body in bodies:
            first_line = str(body or "").splitlines()[0] if body else ""
            match = self._review_re.fullmatch(first_line.strip())
            if match is None:
                continue
            if int(match["pr"]) != pr or match["head"] != head:
                continue
            if match["verdict"] == "approve":
                reviewers.add(match["reviewer"])
        return reviewers

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
        approvers, the declared producer, and whether a trusted record exists for
        this exact head. The whole collection is read-only.
        """

        if pr <= 0:
            raise ReviewEvidenceError("pull request number must be positive")
        head = head.strip().lower()
        if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
            raise ReviewEvidenceError("head must be a 40-character hexadecimal revision")
        bodies = self.trusted_bodies(comments)
        lineage = self._recorded_contributors(bodies, pr=pr)
        exact = self._exact_head_contributors(bodies, pr=pr, head=head)
        approvers = self._exact_head_approvers(bodies, pr=pr, head=head)
        producer_token = producer.strip() if producer is not None else None
        if producer_token == "":
            raise ReviewEvidenceError("producer, when provided, must be a non-empty token")
        return ReviewIndependenceSignals(
            revision=RevisionId(self.repository, head),
            contributors=frozenset(self._worker(worker) for worker in lineage),
            approvers=frozenset(self._worker(worker) for worker in approvers),
            producer=self._worker(producer_token) if producer_token else None,
            provenance_recorded=bool(exact),
        )
