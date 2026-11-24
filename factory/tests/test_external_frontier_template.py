"""Installed GitHub-only read-only frontier is not an authority to claim."""

from __future__ import annotations

from pathlib import Path

from tests.workflow_files import adopter_view

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT.parent / ".github" / "workflows" / "rotisserie-external-frontier.yml"


def test_github_frontier_is_manual_and_read_only() -> None:
    workflow = adopter_view(TEMPLATE.name)
    assert "on:\n  workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "FACTORY_REPO_VARS: >-" in workflow
    assert "uses: ./.github/actions/factory-state-cache" in workflow
    assert "gh issue edit" not in workflow
    assert "git push" not in workflow
    assert "contents: read" in workflow
    assert "issues: read" in workflow
    assert "pull-requests: read" in workflow
    assert "actions: read" in workflow
    assert "contents: write" not in workflow
    assert "issues: write" not in workflow
    assert "pull-requests: write" not in workflow
    assert "checkout@v7" in workflow
    assert "persist-credentials: false" in workflow
    assert "uv tool run --with 'asyncpg>=0.30,<1' --from ./.rotisserie/runtime rotisserie factory" in workflow
    assert 'next --repo "$GITHUB_REPOSITORY" --json' in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "retention-days: 1" in workflow
    assert "No work is reserved." in workflow
    assert "factory-external-claim" not in workflow


def test_installer_inserts_pinned_runtime_before_frontier_query() -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("frontier_installer", ROOT / "install" / "install.py")
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = installer
    spec.loader.exec_module(installer)
    sha = "c" * 40
    caller = (ROOT / "install" / "callers" / TEMPLATE.name).read_text()
    rendered = installer.render(caller, source="org/rotisserie", ref=sha)
    assert f"uses: org/rotisserie/.github/workflows/{TEMPLATE.name}@{sha}" in rendered
    reusable = TEMPLATE.read_text()
    assert "repository: ${{ job.workflow_repository }}" in reusable
    assert "ref: ${{ job.workflow_sha }}" in reusable
    assert "bash .rotisserie/runtime/factory/install/overlay.sh" in reusable
    assert "persist-credentials: false" in reusable
    assert reusable.index("Check out Rotisserie factory runtime") < reusable.index(
        "Read controller-ranked frontier"
    )
