"""The store self-test and export workflows keep their credentials inside one job."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CALLERS = Path(__file__).resolve().parents[1] / "install" / "callers"


def _load(name: str) -> tuple[str, dict]:
    text = (ROOT / name).read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def test_selftest_masks_its_secrets_and_cleans_its_namespace() -> None:
    text, workflow = _load("factory-turso-selftest.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert 'echo "::add-mask::$TURSO_TOKEN"' in text
    assert "umask 077" in text
    assert "FACTORY_TEST_LIBSQL_TOKEN_FILE=" in text
    assert "FACTORY_TEST_LIBSQL_TOKEN=" not in text
    assert '--namespace "selftest-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in text
    assert 'rm -f "$RUNNER_TEMP/factory-store-token"' in text
    assert "factory-turso-selftest-${{ github.run_id }}" in text


def test_export_reads_with_the_read_only_token_for_one_day() -> None:
    text, workflow = _load("factory-state-export.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert "FACTORY_TURSO_READ_TOKEN" in text
    assert "secrets.FACTORY_TURSO_TOKEN " not in text and "secrets.FACTORY_TURSO_TOKEN}" not in text
    steps = workflow["jobs"]["export"]["steps"]
    files = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")]
    assert files[0]["with"]["retention-days"] == 1
    assert files[0]["with"]["name"] == "factory-state-export-${{ github.run_id }}"
    assert files[1]["with"]["name"] == "factory-state-export-summary-${{ github.run_id }}"


def test_migrate_keeps_both_credentials_in_one_job_and_removes_them() -> None:
    text, workflow = _load("factory-store-migrate.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow[True]["workflow_call"]["inputs"]["dry_run"]["default"] is True
    assert list(workflow["jobs"]) == ["migrate"]
    job = workflow["jobs"]["migrate"]
    assert job["concurrency"]["cancel-in-progress"] is False
    steps = job["steps"]
    secret_steps = [s for s in steps if "secrets." in json.dumps(s)]
    assert [s["name"] for s in secret_steps] == ["Give this job the Neon and store connections"]
    connect = secret_steps[0]["run"]
    for value in ("$TURSO_URL", "$TURSO_TOKEN", "$uri"):
        assert f'echo "::add-mask::{value}"' in connect
    assert connect.index("::add-mask::$uri") < connect.index('printf \'%s\' "$uri"')
    assert "umask 077" in connect and "chmod 600" in connect
    assert "::warning::" not in connect and connect.count("exit 1") >= 4
    for line in text.splitlines():
        if "echo" in line and ("$NEON_API_KEY" in line or "$TURSO_TOKEN" in line):
            assert "::add-mask::" in line
    cleanup = next(s for s in steps if s["name"] == "Remove the credential files")
    assert cleanup["if"] == "always()"
    assert "factory-neon-connection" in cleanup["run"] and "factory-store-token" in cleanup["run"]
    migrate = next(s for s in steps if s["name"] == "Migrate the factory state")
    assert "pip install --quiet 'asyncpg>=0.30,<1'" in migrate["run"]
    assert '[[ "$DRY_RUN" != "false" ]]' in migrate["run"]
    assert not any("pip install" in s.get("run", "") for s in steps if s is not migrate)
    upload = steps[-1]
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == "factory-store-migrate-${{ github.run_id }}"
    assert upload["with"]["retention-days"] == 7


def test_the_migrate_caller_defaults_to_a_dry_run() -> None:
    caller = yaml.safe_load((CALLERS / "factory-store-migrate.yml").read_text(encoding="utf-8"))
    assert caller[True]["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    job = caller["jobs"]["rotisserie"]
    assert job["with"]["dry_run"] == "${{ inputs.dry_run == true }}"
    assert set(job["secrets"]) == {"NEON_API_KEY", "FACTORY_TURSO_URL", "FACTORY_TURSO_TOKEN"}
    overlay = (CALLERS.parent / "overlay-paths.txt").read_text(encoding="utf-8").split()
    for name in ("", "_map", "_neon", "_rows"):
        assert f".github/scripts/factory_turso_migrate{name}.py" in overlay
