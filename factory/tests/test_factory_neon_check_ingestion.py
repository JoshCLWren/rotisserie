"""Check completions wake GitHub directly; only review failures enter Neon."""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
operations: Any = importlib.import_module("factory_state_operations")


def review_event(*, conclusion: str = "failure", name: str = "PR-Agent Advisory Review",
                 run_id: int = 123) -> dict[str, Any]:
    return {"workflow_run": {
        "status": "completed", "conclusion": conclusion, "name": name,
        "head_sha": "a" * 40, "id": run_id, "pull_requests": [{"number": 567}],
    }}


class RecordingConnection:
    def __init__(self) -> None:
        self.last_run = 0
        self.statements: list[str] = []

    async def fetchval(self, query: str, repo: str, pr: int, sha: str, run_id: int) -> int | None:
        assert (repo, pr, sha) == ("JoshCLWren/comic-pile", 567, "a" * 40)
        self.statements.append(query)
        if run_id <= self.last_run:
            return None
        self.last_run = run_id
        return 1


def test_review_cooldown_requires_a_restricted_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_STATE_DSN_FILE", raising=False)
    with pytest.raises(RuntimeError, match="requires restricted Neon"):
        operations.ingest_check("JoshCLWren/comic-pile", review_event())


def test_successful_ci_does_not_query_or_write_neon() -> None:
    conn = RecordingConnection()
    for name, conclusion in (
        ("CI", "success"),
        ("Route Fragment Guard", "failure"),
        ("PR-Agent Advisory Review", "success"),
        ("Local Ollama Review", "cancelled"),
    ):
        assert asyncio.run(operations._ingest_check(
            conn, "JoshCLWren/comic-pile",
            review_event(name=name, conclusion=conclusion),
        )) == 0
    assert conn.statements == []


def test_failed_review_is_exact_head_and_idempotent_by_run() -> None:
    conn = RecordingConnection()
    event = review_event()
    assert asyncio.run(operations._ingest_check(conn, "JoshCLWren/comic-pile", event)) == 1
    assert asyncio.run(operations._ingest_check(conn, "JoshCLWren/comic-pile", event)) == 0
    assert asyncio.run(operations._ingest_check(conn, "JoshCLWren/comic-pile", review_event(run_id=124))) == 1
    assert all("factory_pr_stage_retry" in sql for sql in conn.statements)
    assert all("factory_check_projection" not in sql for sql in conn.statements)
    assert all("last_run_id<EXCLUDED.last_run_id" in sql for sql in conn.statements)


def test_check_completion_wakeup_skips_neon_except_review_failure() -> None:
    workflow = (ROOT / ".github/workflows/factory-check-projection.yml").read_text()
    assert "head_repository.full_name == github.repository" in workflow
    assert "pull_requests[0].number != null" in workflow
    assert 'if: env.REVIEW_FAILURE == \'true\'' in workflow
    assert 'neon-api-key: ${{ secrets.NEON_API_KEY }}' in workflow
    assert 'neon-project-id: ${{ vars.NEON_PROJECT_ID }}' in workflow
    assert '[[ -z "${FACTORY_STATE_DSN_FILE:-}" || ! -s "$FACTORY_STATE_DSN_FILE" ]]' in workflow
    assert "ref: main" in workflow and "persist-credentials: false" in workflow
    assert "gh workflow run fixed-model-factory-dispatch.yml" in workflow
    assert 'target=pr:' in workflow
    assert '.head.sha == $sha' in workflow
    assert "gh pr merge" not in workflow
    assert "pull_request_target:" not in workflow
