"""Factory operations Site uses trusted snapshots without inventing roster identities."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"


def _generator():
    path = SCRIPTS / "generate_factory_status_dashboard.py"
    spec = importlib.util.spec_from_file_location("fleet_site_generator_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_site_embeds_trusted_snapshot_without_another_published_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _generator()
    snapshot = {
        "generated_at": "2026-11-16T15:00:00Z",
        "workers": [{"worker": "424242", "runtime_source": "runtime-source",
                     "runtime_model": "publisher/newly-discovered-model"}],
        "candidate_health": {"models": [], "providers": []},
        "retry": {"backoffs": [], "reservations": [], "pr_retries": []},
        "unsafe": "</script><script>alert(1)</script>",
    }
    monkeypatch.setattr(module, "collect_snapshot", lambda: snapshot)
    monkeypatch.setattr(module, "render_dashboard",
                        lambda _: "<!doctype html><html><head></head><body><main>Fallback</main></body></html>")
    output = tmp_path / "index.html"
    monkeypatch.setattr(module, "OUTPUT", output)

    assert module.main() == 0
    page = output.read_text()
    assert '<script id="fleet-snapshot" type="application/json">' in page
    assert "factory_fleet_console" not in page
    assert "publisher/newly-discovered-model" in page
    assert "</script><script>alert(1)</script>" not in page
    assert r"\u003c/script" in page
    assert "Fallback" in page
    assert "No pinned model roster" in page
    assert page.count('id="fleet-snapshot"') == 1
    assert json.loads(page.split('id="fleet-snapshot" type="application/json">', 1)[1]
                      .split("</script>", 1)[0]) == snapshot


def test_site_has_no_model_roster_or_mutating_github_api() -> None:
    js = (SCRIPTS / "factory_fleet_console.cjs").read_text()
    assert "data.candidate_health" in js
    assert "data.workers" in js
    assert "data.retry" in js
    assert "inFlight" in js
    assert "ciFor" in js
    assert "localStorage" in js
    assert "modelIds" not in js
    assert "pinnedModels" not in js
    assert 'method:"POST"' not in js
    assert 'method:"PATCH"' not in js
    assert "create-lease" not in js
    assert "merge_pull_request" not in js


def test_console_javascript_syntax() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js not installed")
    subprocess.run([node, "--check", str(SCRIPTS / "factory_fleet_console.cjs")],
                   check=True, capture_output=True, text=True)
