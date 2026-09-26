"""Translate GitHub API fixture shapes into the provider-neutral graph."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rotisserie.domain import (
    Change,
    ChangeId,
    Check,
    CheckId,
    CheckStatus,
    GraphSnapshot,
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
    WorkState,
)


class ProjectionError(ValueError):
    """A GitHub response is ambiguous, incomplete, or crosses a trust boundary."""


class GitHubProjector:
    """Project one explicitly selected repository; payload text cannot retarget it."""

    def __init__(self, repository: RepositoryId) -> None:
        self.repository = repository

    def project(self, payload: Mapping[str, Any]) -> GraphSnapshot:
        repository = self._repository(payload.get("repository"), "repository")
        if repository != self.repository:
            raise ProjectionError("payload repository does not match configured repository")

        issues = self._records(payload.get("issues"), "issues")
        pulls = self._records(payload.get("pull_requests"), "pull_requests")
        works = tuple(self._work(issue) for issue in issues)
        work_ids = {work.id for work in works}
        dependencies: set[tuple[WorkId, WorkId]] = set()
        for issue in issues:
            dependent = WorkId(self.repository, str(self._integer(issue, "number")))
            for raw_dependency in self._records(issue.get("dependencies", ()), "dependencies"):
                dependency_repo = self._repository(
                    raw_dependency.get("repository", payload["repository"]), "dependency repository"
                )
                if dependency_repo != self.repository:
                    raise ProjectionError("dependency crosses the configured repository boundary")
                prerequisite = WorkId(dependency_repo, str(self._integer(raw_dependency, "number")))
                if prerequisite not in work_ids:
                    raise ProjectionError("dependency references work absent from the snapshot")
                dependencies.add((dependent, prerequisite))

        workers: dict[WorkerId, Worker] = {}
        revisions: dict[RevisionId, Revision] = {}
        changes: list[Change] = []
        checks: list[Check] = []
        reviews: list[Review] = []
        for pull in pulls:
            base = self._repository(pull.get("base_repository", payload["repository"]), "PR base")
            if base != self.repository:
                raise ProjectionError("pull request base does not match configured repository")
            work_id = WorkId(self.repository, str(self._integer(pull, "issue_number")))
            if work_id not in work_ids:
                raise ProjectionError("pull request references work absent from the snapshot")
            number = self._integer(pull, "number")
            head = self._revision(pull, "head_sha")
            revisions[head] = Revision(head)
            producer = self._worker(pull.get("author_login"), workers)
            changes.append(Change(ChangeId(self.repository, str(number)), work_id, head, producer))
            for raw_check in self._records(pull.get("checks", ()), "checks"):
                check_head = self._revision(raw_check, "head_sha")
                revisions[check_head] = Revision(check_head)
                checks.append(
                    Check(
                        CheckId(check_head, self._string(raw_check, "name")),
                        self._check_status(raw_check),
                        bool(raw_check.get("required", True)),
                    )
                )
            for raw_review in self._records(pull.get("reviews", ()), "reviews"):
                review_head = self._revision(raw_review, "commit_id")
                revisions[review_head] = Revision(review_head)
                reviewer = self._worker(raw_review.get("author_login"), workers)
                if reviewer is None:
                    raise ProjectionError("review author is required")
                reviews.append(
                    Review(
                        ReviewId(str(raw_review.get("id", "")).strip()),
                        review_head,
                        reviewer,
                        self._review_decision(raw_review),
                        bool(raw_review.get("required", True)),
                    )
                )

        return GraphSnapshot(
            works=works,
            changes=tuple(changes),
            revisions=tuple(sorted(revisions.values(), key=lambda item: item.id)),
            workers=tuple(sorted(workers.values(), key=lambda item: item.id)),
            checks=tuple(checks),
            reviews=tuple(reviews),
            dependencies=frozenset(dependencies),
        )

    def _work(self, issue: Mapping[str, Any]) -> Work:
        state = str(issue.get("state", "")).upper()
        reason = str(issue.get("state_reason", "")).upper()
        if state == "OPEN":
            work_state = WorkState.OPEN
        elif state == "CLOSED" and reason != "NOT_PLANNED":
            work_state = WorkState.COMPLETED
        elif state == "CLOSED":
            work_state = WorkState.CANCELLED
        else:
            raise ProjectionError(f"unsupported issue state: {state!r}")
        return Work(
            WorkId(self.repository, str(self._integer(issue, "number"))),
            self._string(issue, "title"),
            work_state,
            int(issue.get("priority", 0)),
        )

    def _revision(self, record: Mapping[str, Any], field: str) -> RevisionId:
        value = self._string(record, field)
        invalid_hex = any(character not in "0123456789abcdefABCDEF" for character in value)
        if len(value) != 40 or invalid_hex:
            raise ProjectionError(f"{field} must be a 40-character hexadecimal revision")
        return RevisionId(self.repository, value.lower())

    @staticmethod
    def _worker(value: Any, workers: dict[WorkerId, Worker]) -> WorkerId | None:
        if value is None:
            return None
        login = str(value).strip()
        if not login:
            raise ProjectionError("worker login cannot be empty")
        worker = WorkerId("github-login", login.casefold())
        workers.setdefault(worker, Worker(worker, human=True))
        return worker

    @staticmethod
    def _check_status(record: Mapping[str, Any]) -> CheckStatus:
        status = str(record.get("status", "")).upper()
        conclusion = str(record.get("conclusion") or "").upper()
        if status != "COMPLETED" or not conclusion:
            return CheckStatus.PENDING
        if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            return CheckStatus.PASSED
        return CheckStatus.FAILED

    @staticmethod
    def _review_decision(record: Mapping[str, Any]) -> ReviewDecision:
        state = str(record.get("state", "")).upper()
        decisions = {
            "APPROVED": ReviewDecision.APPROVED,
            "CHANGES_REQUESTED": ReviewDecision.CHANGES_REQUESTED,
            "PENDING": ReviewDecision.PENDING,
            "COMMENTED": ReviewDecision.PENDING,
            "DISMISSED": ReviewDecision.PENDING,
        }
        try:
            return decisions[state]
        except KeyError as error:
            raise ProjectionError(f"unsupported review state: {state!r}") from error

    @staticmethod
    def _records(value: Any, field: str) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ProjectionError(f"{field} must be a sequence")
        if not all(isinstance(item, Mapping) for item in value):
            raise ProjectionError(f"{field} must contain objects")
        return tuple(value)

    @staticmethod
    def _string(record: Mapping[str, Any], field: str) -> str:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ProjectionError(f"{field} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _integer(record: Mapping[str, Any], field: str) -> int:
        value = record.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ProjectionError(f"{field} must be a positive integer")
        return value

    @staticmethod
    def _repository(value: Any, field: str) -> RepositoryId:
        if not isinstance(value, Mapping):
            raise ProjectionError(f"{field} must be an object")
        try:
            return RepositoryId(str(value["host"]), str(value["owner"]), str(value["name"]))
        except (KeyError, ValueError) as error:
            raise ProjectionError(f"invalid {field}") from error
