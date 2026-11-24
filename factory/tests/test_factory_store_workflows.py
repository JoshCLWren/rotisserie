"""The store self-test and export workflows keep their credentials inside one job."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2] / ".github" / "workflows"


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
