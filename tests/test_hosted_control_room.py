"""Shared hosted Rotisserie UI and client-side security contract."""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "factory" / "scripts" / "build_hosted_control_room.py"
ADAPTER = ROOT / "factory" / "scripts" / "hosted_control_room.js"
WORKFLOW = ROOT / "factory" / ".github" / "workflows" / "rotisserie-operator.yml"


def builder():
    spec = importlib.util.spec_from_file_location("rotisserie_hosted_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hosted_page_reuses_canonical_local_ui_without_local_session():
    page = builder().render("JoshCLWren/comic-pile")
    assert "Rotisserie · Control room" in page
    assert 'data-view="queue"' in page
    assert 'data-view="workers"' in page
    assert 'data-view="health"' in page
    assert 'id="github-connect"' in page
    assert "window.hostedRequest(path,body)" in page
    assert "__SESSION_TOKEN__" not in page
    assert "__REPOSITORY__" not in page
    assert 'const TOKEN=null, REPO="JoshCLWren/comic-pile";' in page
    assert "GitHub Pages · Published snapshot" in page
    assert "https://api.github.com" in page
    assert "https://raw.githubusercontent.com" in page
    assert "localStorage" not in page


@pytest.mark.parametrize(
    "name", ["../bad", "owner", "owner/repo/extra", "owner/<repo>"]
)
def test_hosted_page_rejects_invalid_repo(name):
    with pytest.raises(ValueError):
        builder().render(name)


def test_hosted_scripts_parse_without_dependencies(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node not installed")
    page = builder().render("JoshCLWren/comic-pile")
    scripts = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
    assert len(scripts) >= 2
    for i, script in enumerate(scripts):
        path = tmp_path / f"hosted-{i}.js"
        path.write_text(script, encoding="utf-8")
        subprocess.run(
            [node, "--check", str(path)], check=True, capture_output=True, text=True
        )


def test_workflow_requires_verified_actor_before_executing_commands():
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "github.actor_id" in content
    assert "ROTISSERIE_ADMIN_GITHUB_IDS" in content
    assert "refs/heads/main" in content
    assert content.index("Authorize GitHub actor") < content.index(
        "Check out Rotisserie"
    )
    assert "write_command(body)" in content
    assert "subprocess.run(command" in content
    assert "shell=True" not in content
    assert "persist-credentials: false" in content


def test_browser_uses_no_persisted_token_and_only_dispatches_to_actions():
    content = ADAPTER.read_text(encoding="utf-8")
    assert "localStorage" not in content
    assert "sessionStorage" not in content
    assert "window.hostedRequest" in content
    assert "/actions/workflows/" in content
    assert "/dispatches" in content
    assert "repo.permissions.push" in content
    assert "published.admin_ids" in content
