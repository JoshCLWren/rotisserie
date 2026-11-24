"""Regression coverage for factory PR rebase fanout."""

from pathlib import Path

from tests.workflow_files import adopter_view

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT.parent / ".github" / "workflows" / "auto-rebase-open-prs.yml"


def test_mass_rebase_requires_explicit_dispatch() -> None:
    """Main merges cannot rewrite every open factory PR head."""
    source = adopter_view(WORKFLOW.name)
    triggers = source.split("on:\n", maxsplit=1)[1].split("\npermissions:", maxsplit=1)[0]

    assert "workflow_dispatch:" in triggers
    assert "\n  push:" not in triggers
    assert 'gh pr update-branch "${pr}"' in source
