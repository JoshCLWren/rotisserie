"""Check completions wake GitHub directly; only review failures enter the factory store."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
from tests import turso_support as support  # noqa: E402

operations: Any = importlib.import_module("factory_state_operations")


def review_event(
    *, conclusion: str = "failure", name: str = "PR-Agent Advisory Review", run_id: int = 123
) -> dict[str, Any]:
    return {
        "workflow_run": {
            "status": "completed",
            "conclusion": conclusion,
            "name": name,
            "head_sha": "a" * 40,
            "id": run_id,
            "pull_requests": [{"number": 567}],
        }
    }


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def _retry_rows(db: Any, repo: str) -> list[dict[str, Any]]:
    return db.read(
        "SELECT pr_number, head_sha, stage, attempts, last_run_id FROM factory_pr_stage_retry"
        " WHERE repo = :r",
        {"r": repo},
    )


def test_review_cooldown_requires_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    with pytest.raises(RuntimeError, match="requires the factory store"):
        operations.ingest_check("acme/widgets", review_event())


def test_successful_ci_does_not_write_the_store(env) -> None:
    db, repo = env
    for name, conclusion in (
        ("CI", "success"),
        ("Route Fragment Guard", "failure"),
        ("PR-Agent Advisory Review", "success"),
        ("Local Ollama Review", "cancelled"),
    ):
        assert operations.ingest_check(repo, review_event(name=name, conclusion=conclusion)) == 0
    assert _retry_rows(db, repo) == []


def test_failed_review_is_exact_head_and_idempotent_by_run(env) -> None:
    db, repo = env
    event = review_event()
    assert operations.ingest_check(repo, event) == 1
    assert operations.ingest_check(repo, event) == 0
    assert operations.ingest_check(repo, review_event(run_id=122)) == 0
    assert operations.ingest_check(repo, review_event(run_id=124)) == 1
    assert _retry_rows(db, repo) == [
        {
            "pr_number": 567,
            "head_sha": "a" * 40,
            "stage": "factory:review",
            "attempts": 2,
            "last_run_id": 124,
        }
    ]
    assert operations.cooling_prs(repo, {567: ("a" * 40, "factory:review")}) == {567}


def test_check_completion_wakeup_skips_the_store_except_review_failure() -> None:
    workflow = (ROOT.parent / ".github/workflows/factory-check-projection.yml").read_text()
    assert "head_repository.full_name == github.repository" in workflow
    assert "pull_requests[0].number != null" in workflow
    assert "if: env.REVIEW_FAILURE == 'true'" in workflow
    assert "token: ${{ secrets.FACTORY_TURSO_TOKEN }}" in workflow
    assert '[[ -z "${FACTORY_TURSO_TOKEN_FILE:-}" || ! -s "$FACTORY_TURSO_TOKEN_FILE" ]]' in workflow
    assert "ref: main" in workflow and "persist-credentials: false" in workflow
    assert "gh workflow run fixed-model-factory-dispatch.yml" in workflow
    assert "target=pr:" in workflow
    assert ".head.sha == $sha" in workflow
    assert "gh pr merge" not in workflow
    assert "pull_request_target:" not in workflow
