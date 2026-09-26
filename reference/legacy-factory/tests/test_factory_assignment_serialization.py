"""Regression coverage for factory assignment-writer serialization.

Incident #2727 proved that independent workflow concurrency groups let two
controllers observe the same worker as idle and lease unrelated targets to it.
Every workflow with assignment authority must therefore share one repository-
wide serialization group.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ASSIGNMENT_GROUP = "group: fixed-model-factory-dispatch"


def test_all_factory_assignment_writers_share_one_concurrency_group() -> None:
    """No assignment-capable workflow may mutate leases outside the shared lock."""
    writers = {
        "fixed-model-factory-dispatch.yml": 'python3 "$controller" assign --worker "$worker"',
        "fixed-model-factory-capacity-refill.yml": 'python3 "$controller" assign --worker "$worker"',
        "factory-completion-drain.yml": "factory_full_completion_controller.py",
    }

    for filename, assignment_marker in writers.items():
        text = (WORKFLOWS / filename).read_text(encoding="utf-8")
        assert assignment_marker in text, f"{filename} no longer matches its assignment marker"
        assert ASSIGNMENT_GROUP in text, (
            f"{filename} can assign factory work without the shared serialization group"
        )


def test_incident_writer_groups_are_not_independent() -> None:
    """The two writer groups implicated in incident #2727 must stay retired."""
    completion = (WORKFLOWS / "factory-completion-drain.yml").read_text(
        encoding="utf-8"
    )
    refill = (WORKFLOWS / "fixed-model-factory-capacity-refill.yml").read_text(
        encoding="utf-8"
    )

    assert "group: factory-completion-drain" not in completion
    assert "group: fixed-model-capacity-refill-${{ github.event.workflow_run.id || github.run_id }}" not in refill
