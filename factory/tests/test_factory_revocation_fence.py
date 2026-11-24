"""Regression coverage for the fixed-model factory revocation fence."""

from pathlib import Path

from tests.workflow_files import adopter_view

ROOT = Path(__file__).resolve().parents[1]
FENCE = ROOT.parent / ".github/workflows/factory-revocation-fence.yml"


def test_revocation_fence_only_validates_factory_pr_queue_entry() -> None:
    """Shared repair pushes must not be judged by the original producer lease."""
    workflow = adopter_view(FENCE.name)

    assert "types: [opened, reopened, ready_for_review]" in workflow
    assert "synchronize" not in workflow
    assert "factory/<worker>-<issue>-<suffix>" in workflow
    assert "no longer owns open issue" in workflow
