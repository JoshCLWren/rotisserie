"""The estimate-history query on the factory store, summarized as the console publishes it.

Ported from the PostgreSQL estimate history suite.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests import turso_support as support

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rotisserie.factory_console.service import estimate_neon  # noqa: E402

events = importlib.import_module("factory_turso_events")


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def _event(repo: str, kind: str, subject: int, n: int, **detail: Any) -> Any:
    origin = "rotisserie-external" if kind.startswith("external_") else "github-actions"
    return support.core.event_insert(
        repo,
        origin,
        kind,
        {"event_id": f"{repo}:{kind}:{n}", **detail},
        subject_type="target" if kind.startswith("external_") else "worker",
        subject_number=subject,
    )


def _history(repo: str, cap: int) -> dict[str, object]:
    rows = events.history_rows(repo, 2, cap + 1, estimate_neon.EVENT_TYPES)
    return estimate_neon.summarize(rows, days=2, cap=cap, now=datetime.now(UTC))


def test_the_query_collapses_refusals_and_reads_only_this_repository(env) -> None:
    db, repo = env
    other = f"{repo}-other"
    statements = [
        _event(repo, "worker_assigned", 3, 0, run_id="70", targets=[5, 1005], claim_id="secret")
    ]
    statements += [
        _event(
            repo,
            "model_attempt",
            3,
            index,
            run_id="70",
            selected_number=5,
            outcome_class="model_unavailable",
            started_at=f"2026-11-24T10:00:00.{index:07d}Z",
            failure_log_tail="raw tail",
        )
        for index in range(150)
    ]
    statements += [
        _event(
            repo,
            "model_attempt",
            3,
            999,
            run_id="70",
            selected_number=5,
            outcome_class="success",
            duration_ms=90000,
            agent_exit_code=0,
        ),
        _event(
            repo, "worker_completed", 3, 0, run_id="70", selected_number=5, outcome_class="success"
        ),
        _event(repo, "external_reserved", 5, 0, client="label", actor="owner"),
        _event(other, "model_attempt", 3, 0, run_id="71", selected_number=5),
    ]
    try:
        db.batch(statements)
        data = _history(repo, 100)
        assert data["status"] == "ok", data
        assert data["rows_read"] == 5 and data["truncated"] is False
        entry = data["numbers"]["5"]  # type: ignore[index]
        assert [run["run_id"] for run in entry["runs"]] == ["70"]
        run = entry["runs"][0]
        assert run["reached_model"] is True and run["refusals"] == 150
        assert entry["counts"] == {"owner_claim": 1}
        text = json.dumps(data)
        for private in ("secret", "raw tail", "owner", other):
            assert private not in text.replace("owner_claim", "")
    finally:
        support.cleanup(db, other)


def test_the_row_cap_marks_the_history_truncated(env) -> None:
    db, repo = env
    db.batch(
        [
            _event(
                repo,
                "model_attempt",
                3,
                run,
                run_id=str(run),
                selected_number=9,
                outcome_class="success",
                duration_ms=10,
            )
            for run in range(120)
        ]
    )
    data = _history(repo, 100)
    assert data["truncated"] is True and data["rows_read"] == 100
