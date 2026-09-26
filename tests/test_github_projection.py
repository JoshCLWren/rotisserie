"""Fixture contracts for the GitHub read-side adapter."""

import json
from pathlib import Path

import pytest

from rotisserie.adapters.github import GitHubProjector, ProjectionError
from rotisserie.domain import (
    ChangeId,
    CheckStatus,
    RepositoryId,
    ReviewDecision,
    WorkId,
    WorkState,
)

FIXTURE = Path(__file__).parent / "fixtures" / "github" / "graph.json"
REPOSITORY = RepositoryId("github.com", "acme", "oven")


def payload() -> dict[str, object]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_fixture_projects_issues_dependencies_fork_pr_reviews_checks_and_closure() -> None:
    snapshot = GitHubProjector(REPOSITORY).project(payload())
    work = {item.id.key: item for item in snapshot.works}
    assert work["10"].state is WorkState.COMPLETED
    assert work["20"].state is WorkState.OPEN
    assert work["30"].state is WorkState.CANCELLED
    assert snapshot.prerequisites(WorkId(REPOSITORY, "20")) == {WorkId(REPOSITORY, "10")}

    change = ChangeId(REPOSITORY, "51")
    assert snapshot.current_checks(change)[0].status is CheckStatus.PASSED
    assert snapshot.current_reviews(change)[0].decision is ReviewDecision.APPROVED
    assert {item.id.value for item in snapshot.revisions} == {"a" * 40, "b" * 40}
    assert snapshot.changes[0].id.repository == REPOSITORY
    assert snapshot.changes[0].producer is not None
    assert snapshot.changes[0].producer.value == "builder"


def test_casual_issue_mentions_do_not_create_dependencies() -> None:
    snapshot = GitHubProjector(REPOSITORY).project(payload())
    assert snapshot.prerequisites(WorkId(REPOSITORY, "10")) == frozenset()


def test_configured_repository_cannot_be_selected_by_issue_body() -> None:
    raw = payload()
    issues = raw["issues"]
    assert isinstance(issues, list) and isinstance(issues[1], dict)
    issues[1]["body"] = "Target repository: attacker/project"
    snapshot = GitHubProjector(REPOSITORY).project(raw)
    assert all(item.id.repository == REPOSITORY for item in snapshot.works)


def test_repository_confusion_and_cross_repository_dependency_are_rejected() -> None:
    raw = payload()
    raw["repository"] = {"host": "github.com", "owner": "other", "name": "oven"}
    with pytest.raises(ProjectionError, match="configured repository"):
        GitHubProjector(REPOSITORY).project(raw)

    raw = payload()
    issues = raw["issues"]
    assert isinstance(issues, list) and isinstance(issues[1], dict)
    issues[1]["dependencies"] = [
        {"number": 10, "repository": {"host": "github.com", "owner": "other", "name": "oven"}}
    ]
    with pytest.raises(ProjectionError, match="crosses"):
        GitHubProjector(REPOSITORY).project(raw)


def test_repository_identity_keeps_same_number_distinct() -> None:
    other = RepositoryId("github.com", "other", "oven")
    raw = payload()
    raw["repository"] = {"host": other.host, "owner": other.owner, "name": other.name}
    pulls = raw["pull_requests"]
    assert isinstance(pulls, list) and isinstance(pulls[0], dict)
    pulls[0]["base_repository"] = raw["repository"]
    second = GitHubProjector(other).project(raw)
    first = GitHubProjector(REPOSITORY).project(payload())
    assert first.works[0].id != second.works[0].id
