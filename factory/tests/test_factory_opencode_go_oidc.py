"""Go CI identity is minted from GitHub OIDC and never a static API key."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/factory_opencode_go_oidc.sh"
RUNNER = ROOT.parent / ".github/workflows/free-model-factory-run.yml"
ENTRY = ROOT.parent / ".github/workflows/free-model-factory-entry.yml"
DISPATCH = ROOT.parent / ".github/workflows/fixed-model-factory-dispatch.yml"


@pytest.fixture
def fake_curl(tmp_path: Path) -> Path:
    curl = tmp_path / "curl"
    curl.write_text(
        '#!/usr/bin/env bash\n'
        'if [[ "$*" == *"/console/auth/oauth/token"* ]]; then\n'
        "  printf '%s\\n' '{\"access_token\":\"short-lived-test-token\"}'\n"
        'else\n'
        "  printf '%s\\n' '{\"value\":\"signed-github-test-oidc\"}'\n"
        'fi\n'
    )
    curl.chmod(0o755)
    return curl


def test_oidc_flow_mints_runtime_key_without_exposing_id_token(fake_curl: Path) -> None:
    env = {
        **os.environ,
        "PATH": f"{fake_curl.parent}:{os.environ['PATH']}",
        "OPENCODE_GO_CLIENT_ID": "synthetic-service-client",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-github-request-token",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.githubusercontent.com/id?test=1",
        "OPENCODE_API_KEY": "stale-zen-key",
    }
    script = (
        f'source "{SCRIPT}"\n'
        'factory_opencode_go_oidc || exit 9\n'
        '[[ "$OPENCODE_API_KEY" == "short-lived-test-token" ]] || exit 8\n'
        "echo federation-ok\n"
    )
    result = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "federation-ok" in result.stdout
    assert "::add-mask::short-lived-test-token" in result.stdout
    assert "private-github-request-token" not in result.stdout + result.stderr
    assert "signed-github-test-oidc" not in result.stdout + result.stderr
    assert "synthetic-service-client" not in result.stdout + result.stderr
    assert "stale-zen-key" not in result.stdout + result.stderr


def test_missing_oidc_context_never_uses_stale_zen_token(fake_curl: Path) -> None:
    env = {
        **os.environ,
        "PATH": f"{fake_curl.parent}:{os.environ['PATH']}",
        "OPENCODE_GO_CLIENT_ID": "synthetic-service-client",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "",
        "OPENCODE_API_KEY": "stale-zen-key",
    }
    script = (
        f'source "{SCRIPT}"\n'
        'if factory_opencode_go_oidc; then exit 7; fi\n'
        '[[ -z "${OPENCODE_API_KEY:-}" ]] || exit 8\n'
    )
    result = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_reusable_job_and_entry_explicitly_grant_oidc_only_to_model_job() -> None:
    entry = yaml.safe_load(ENTRY.read_text())
    runner = yaml.safe_load(RUNNER.read_text())
    assert entry["permissions"]["id-token"] == "write"
    assert runner["jobs"]["factory"]["permissions"]["id-token"] == "write"
    assert runner["jobs"]["validate_assignment"]["permissions"].get("id-token") is None
    assert "OPENCODE_GO_API_KEY" not in RUNNER.read_text()
    assert "secrets.OPENCODE_GO_CLIENT_ID" in RUNNER.read_text()
    assert 'factory_opencode_go_oidc.sh' in RUNNER.read_text()
    assert "OPENCODE_GO_CLIENT_ID != ''" in RUNNER.read_text()
    assert "FACTORY_HAS_OPENCODE_GO_CLIENT_ID" in DISPATCH.read_text()
    assert "secrets.OPENCODE_GO_API_KEY" not in DISPATCH.read_text()


def test_worker_renews_oidc_from_trusted_staged_script() -> None:
    worker = (ROOT / ".github/scripts/free-model-factory-worker.sh").read_text()
    assert 'cp .github/scripts/factory_opencode_go_oidc.sh' in worker
    assert 'source "$helper"' in worker
    assert "factory_opencode_go_refresh" in worker
    assert "blocked_providers+=(opencode-free)" in worker
