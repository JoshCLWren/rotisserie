"""Trace the accepted dependency prototype into explicit graph edges."""

from prototype.dependency_policy import parse_dependency_numbers
from rotisserie.domain import GraphSnapshot, RepositoryId, Work, WorkId


def test_prototype_dependencies_map_to_domain_relationships() -> None:
    repository = RepositoryId("forge.example", "team", "project")
    dependent = WorkId(repository, "30")
    declared = parse_dependency_numbers("Depends on #10, #20; context in #99")
    prerequisites = {WorkId(repository, str(number)) for number in declared}
    snapshot = GraphSnapshot(
        works=(
            Work(dependent, "Child"),
            *(Work(item, f"Prerequisite {item.key}") for item in prerequisites),
        ),
        dependencies=frozenset((dependent, item) for item in prerequisites),
    )
    assert snapshot.prerequisites(dependent) == prerequisites
