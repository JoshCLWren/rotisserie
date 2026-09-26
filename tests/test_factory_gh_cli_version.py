"""Regression coverage for the factory GitHub CLI compatibility setup."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
GH_VERSION = "2.97.0"
GH_LINUX_AMD64_SHA256 = "a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112"


def test_factory_gate_workflows_ensure_json_capable_github_cli() -> None:
    """Every runtime that invokes mechanical gates installs a JSON-capable gh first."""
    for name in ("fixed-model-factory-dispatch.yml", "factory-ready-merge-drain.yml"):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        setup = text.index("Ensure GitHub CLI supports JSON PR checks")
        drain = text.index("Drain exact-head ready factory PRs")

        assert setup < drain
        assert "gh pr checks --help" in text
        assert f"version='{GH_VERSION}'" in text
        assert GH_LINUX_AMD64_SHA256 in text
        assert "sha256sum --check --strict" in text


def test_dispatcher_selects_upgraded_gh_before_installing_rest_shim() -> None:
    """The dispatcher shim delegates to the upgraded CLI, not the runner's old binary."""
    text = (WORKFLOW_DIR / "fixed-model-factory-dispatch.yml").read_text(encoding="utf-8")

    setup = text.index("Ensure GitHub CLI supports JSON PR checks")
    shim = text.index("Install REST-backed factory GitHub reads")
    assert setup < shim
