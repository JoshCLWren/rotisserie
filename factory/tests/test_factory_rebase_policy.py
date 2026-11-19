"""Regression coverage for factory PR rebase fanout."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-rebase-open-prs.yml"


def test_mass_rebase_requires_explicit_dispatch() -> None:
    """Main merges cannot rewrite every open factory PR head."""
    source = WORKFLOW.read_text(encoding="utf-8")
    triggers = source.split("on:\n", maxsplit=1)[1].split("\npermissions:", maxsplit=1)[0]

    assert "workflow_dispatch:" in triggers
    assert "\n  push:" not in triggers
    assert 'gh pr update-branch "${pr}"' in source
