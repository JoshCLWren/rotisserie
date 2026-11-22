"""The GitHub claim bridge must retain authority only inside trusted Actions."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/rotisserie-external-claim.yml"


def test_manual_neon_claim_workflow_is_a_controller_not_a_factory_worker() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "on:\n  workflow_dispatch:" in content
    assert "schedule:" not in content
    assert "workflow_run:" not in content
    assert "issue_comment:" not in content
    assert "contents: read" in content
    assert "issues: write" in content  # verified PR handoff labels only
    assert "pull-requests: read" in content
    assert "contents: write" not in content
    assert "pull-requests: write" not in content
    assert "NEON_API_KEY" in content
    assert "uses: ./.github/actions/factory-state-cache" in content
    assert "test -s \"$FACTORY_STATE_DSN_FILE\"" in content
    assert "GITHUB_RUN_ATTEMPT" in content
    assert "github.triggering_actor" in content
    assert "persist-credentials: false" in content
    assert "FACTORY_REPO_VARS: >-" in content
    assert "factory \"$OP\"" in content
    assert "uv tool run --with 'asyncpg>=0.30,<1' --from ./.rotisserie/runtime rotisserie" in content
    assert "retention-days: 1" in content
    assert "git push" not in content
    assert "gh issue edit" not in content


def test_installed_workflow_has_pinned_authoritative_runtime() -> None:
    path = ROOT / "install/install.py"
    spec = importlib.util.spec_from_file_location("external_claim_installer", path)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = installer
    spec.loader.exec_module(installer)
    sha = "f" * 40
    rendered = installer.render(WORKFLOW.read_text(), source="org/rotisserie", ref=sha)
    assert f"ref: {sha}" in rendered
    assert "repository: org/rotisserie" in rendered
    assert "bash .rotisserie/runtime/factory/install/overlay.sh" in rendered
    assert rendered.index("Check out Rotisserie factory runtime") < rendered.index(
        "Configure shared restricted Neon authority"
    )
    assert "persist-credentials: false" in rendered
