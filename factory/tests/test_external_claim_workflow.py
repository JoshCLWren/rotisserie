"""The GitHub claim bridge must retain authority only inside trusted Actions."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests.workflow_files import adopter_view

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT.parent / ".github/workflows/rotisserie-external-claim.yml"


def test_manual_neon_claim_workflow_is_a_controller_not_a_factory_worker() -> None:
    content = adopter_view(WORKFLOW.name)
    assert "on:\n  workflow_dispatch:" in content
    assert "schedule:" not in content
    assert "workflow_run:" not in content
    assert "issue_comment:" not in content
    assert "contents: read" in content
    assert "issues: write" in content  # verified handoff labels
    assert "pull-requests: write" in content  # trusted author comment on the PR
    assert "checks: read" in content
    assert "contents: write" not in content
    assert "actions: write" not in content
    # The bridge never runs PR code: it checks out main and the pinned runtime.
    assert "ref: main" in content
    assert "github.event.pull_request" not in content
    assert "FACTORY_TURSO_TOKEN" in content
    assert "uses: ./.github/actions/factory-store" in content
    assert "test -s \"$FACTORY_TURSO_TOKEN_FILE\"" in content
    assert "GITHUB_RUN_ATTEMPT" in content
    assert "github.triggering_actor" in content
    assert "persist-credentials: false" in content
    assert "FACTORY_REPO_VARS: >-" in content
    assert "factory \"$OP\"" in content
    assert "uv tool run --from ./.rotisserie/runtime rotisserie" in content
    assert "asyncpg" not in content
    assert "retention-days: 1" in content
    assert "git push" not in content
    assert "gh issue edit" not in content


def test_installed_workflow_has_pinned_authoritative_runtime() -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("external_claim_installer", ROOT / "install" / "install.py")
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = installer
    spec.loader.exec_module(installer)
    sha = "f" * 40
    caller = (ROOT / "install" / "callers" / WORKFLOW.name).read_text()
    rendered = installer.render(caller, source="org/rotisserie", ref=sha)
    assert f"uses: org/rotisserie/.github/workflows/{WORKFLOW.name}@{sha}" in rendered
    reusable = WORKFLOW.read_text()
    assert "repository: ${{ job.workflow_repository }}" in reusable
    assert "ref: ${{ job.workflow_sha }}" in reusable
    assert "bash .rotisserie/runtime/factory/install/overlay.sh" in reusable
    assert "persist-credentials: false" in reusable
    assert reusable.index("Check out Rotisserie factory runtime") < reusable.index(
        "Configure the shared factory store"
    )
