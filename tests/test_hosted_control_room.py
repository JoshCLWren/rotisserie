"""Shared hosted Rotisserie UI and client-side security contract."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "factory" / "scripts" / "build_hosted_control_room.py"
ADAPTER = ROOT / "factory" / "scripts" / "hosted_control_room.js"
PARTS = sorted((ROOT / "factory" / "scripts" / "hosted").glob("*.js"))
WORKFLOW = ROOT / ".github" / "workflows" / "rotisserie-operator.yml"


def builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rotisserie_hosted_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hosted_page_reuses_canonical_local_ui_without_local_session() -> None:
    page = builder().render("acme/widgets")
    assert "Rotisserie · Control room" in page
    assert 'data-view="queue"' in page
    assert 'data-view="workers"' in page
    assert 'data-view="health"' in page
    assert 'id="github-connect"' in page
    assert "window.hostedRequest(path,body)" in page
    assert "__SESSION_TOKEN__" not in page
    assert "__REPOSITORY__" not in page
    assert 'const TOKEN=null, REPO="acme/widgets";' in page
    assert "GitHub Pages · Published snapshot for public views" in page
    assert 'id="hosted-live"' in page
    assert 'const AUTH_BASE = "";' in page
    assert "Sign-in is not configured." in page
    assert "https://raw.githubusercontent.com" in page
    # Only panel read results persist; the dashboard session stays in sessionStorage.
    assert page.count("localStorage.setItem(") == 1
    assert "localStorage.setItem(key,blob)" in page
    assert "sessionStorage.setItem(SESSION_KEY" in page
    assert "Sign in with GitHub" in page
    assert "personal-access-tokens" not in page
    assert "Paste a short-lived" not in page


@pytest.mark.parametrize("name", ["../bad", "owner", "owner/repo/extra", "owner/<repo>"])
def test_hosted_page_rejects_invalid_repo(name: str) -> None:
    with pytest.raises(ValueError):
        builder().render(name)


def test_hosted_scripts_parse_without_dependencies(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node not installed")
    page = builder().render("acme/widgets")
    scripts = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
    assert len(scripts) >= 2
    for i, script in enumerate(scripts):
        path = tmp_path / f"hosted-{i}.js"
        path.write_text(script, encoding="utf-8")
        subprocess.run([node, "--check", str(path)], check=True, capture_output=True, text=True)


def test_workflow_requires_verified_actor_before_executing_commands() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "github.actor_id" in content
    assert "ROTISSERIE_ADMIN_GITHUB_IDS" in content
    assert "refs/heads/main" in content
    assert content.index("Authorize GitHub actor") < content.index("Check out Rotisserie")
    assert "python3 -m rotisserie.hosted_operator write --output" in content
    assert "shell=True" not in content
    assert "persist-credentials: false" in content


def test_browser_uses_real_github_oauth_not_pasted_tokens() -> None:
    content = "\n".join(path.read_text(encoding="utf-8") for path in (*PARTS, ADAPTER))
    assert "localStorage" not in content
    assert "window.prompt" not in content
    assert "personal-access-tokens" not in content
    assert "rotisserie_session" in content
    assert 'location.assign(AUTH_BASE + "/start")' in content
    assert 'authRequest("/session")' in content
    assert 'authRequest("/dispatch"' in content
    assert "history.replaceState" in content
    assert "sessionStorage" in content
    assert "window.hostedRequest" in content
    assert 'authRequest("/runs/"' in content
    assert "/actions/workflows/" not in content
    assert "Bearer " in content


def test_configured_sign_in_service_is_the_only_extra_connect_origin() -> None:
    page = builder().render("acme/widgets", "https://auth.widgets.example/api/factory/github/")
    assert 'const AUTH_BASE = "https://auth.widgets.example/api/factory/github";' in page
    assert "connect-src https://raw.githubusercontent.com https://auth.widgets.example;" in page


@pytest.mark.parametrize("url", ["http://auth.example", "https://auth.example/a b", "javascript:x"])
def test_sign_in_service_must_be_a_plain_https_url(url: str) -> None:
    with pytest.raises(ValueError):
        builder().render("acme/widgets", url)
