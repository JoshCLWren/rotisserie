"""Contract tests for the factory model discovery workflow."""

import re
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "factory-model-discovery.yml"
)
FACTORY_RUN = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "free-model-factory-run.yml"
)
PINNED_OPENCODE_VERSION = "1.18.32"
PINNED_OPENCODE_SHA256 = "3046e0404fdc60fb80307e7a47824ba07477364178a4d09baa8548496dd6d43b"


def test_discovery_workflow_is_scheduled_and_dispatchable() -> None:
    """Operators can dispatch discovery; the schedule does not wait for Harvy."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "cron: '17 */6 * * *'" in workflow
    assert "factory_model_retirement.py" in workflow
    assert "factory/model-retirement" in workflow
    assert "validate-free-model-factories.py" in workflow


def test_discovery_uses_opencode_cli_not_omniroute_or_integrate_api() -> None:
    """Roster truth is OpenCode CLI catalogs, not OmniRoute or NVIDIA HTTP."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "opencode models nvidia" in workflow
    assert "integrate.api.nvidia.com" in workflow
    assert "never integrate.api.nvidia.com" in workflow
    assert "omniroute/auto" not in workflow
    assert "auto/best-free" not in workflow
    assert "OPENCODE_ZEN_API_KEY" in workflow
    assert "NVIDIA_API_KEY" in workflow
    assert "catalog_fixture" in workflow


def _opencode_pin(text: str) -> tuple[str, str]:
    """Extract the pinned OpenCode version and linux-x64 sha256."""
    version = re.search(r"OPENCODE_VERSION: '([^']+)'", text)
    sha = re.search(r"OPENCODE_LINUX_X64_SHA256: '([^']+)'", text)
    assert version is not None
    assert sha is not None
    return version.group(1), sha.group(1)


def test_discovery_and_factory_run_pin_the_same_opencode_release() -> None:
    """Discovery and factory-run must share the live-audit OpenCode CLI pin."""
    discovery_version, discovery_sha = _opencode_pin(WORKFLOW.read_text(encoding="utf-8"))
    run_version, run_sha = _opencode_pin(FACTORY_RUN.read_text(encoding="utf-8"))

    assert discovery_version == PINNED_OPENCODE_VERSION
    assert run_version == PINNED_OPENCODE_VERSION
    assert discovery_sha == PINNED_OPENCODE_SHA256
    assert run_sha == PINNED_OPENCODE_SHA256
    assert discovery_version == run_version
    assert discovery_sha == run_sha


def test_discovery_adds_unused_free_by_default() -> None:
    """Scheduled discovery pins unused free OpenCode models; adds are not opt-in."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "add_unused_free" in workflow
    assert "default: true" in workflow
    assert "--no-add-unused-free" in workflow
    assert "steps.plan.outputs.add" in workflow
    assert ".github/free-model-factories.tsv" in workflow
    assert "they are not auto-added" not in workflow
    assert "Unused free models are listed for operators" not in workflow
    assert "Added to the TSV" in workflow
    assert "Surplus `big-pickle`" in workflow
    assert "Unused `big-pickle` is never grown as a first pin." in workflow
    assert "ADD_UNUSED_FREE" in workflow


def test_discovery_consumes_nvidia_410_markers_from_issue_1093() -> None:
    """Discovery fetches the same #1093 comments the NVIDIA probe uses."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    runner = FACTORY_RUN.read_text(encoding="utf-8")

    assert "issues/1093/comments" in workflow
    assert "--retirement-comments" in workflow
    assert "factory-model-retired-410:v1" in workflow
    assert "retirement_marker='<!-- factory-model-retired-410:v1 -->'" in runner
    assert 'gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/1093/comments?per_page=100"' in runner
