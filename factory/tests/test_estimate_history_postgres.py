"""The estimate-history query (#238) against a real PostgreSQL server.

Uses the disposable server fixture of the external claim suite. Set
``FACTORY_TEST_POSTGRES_URL`` to a LOCAL throwaway server when no server
binaries are installed; the fixture drops and recreates ``factory_state``.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

from tests.test_external_claim_postgres import _sql, neon, pytestmark, server  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rotisserie.factory_console.service import estimate_neon  # noqa: E402

ledger = importlib.import_module("factory_state_ledger")
REPO = "acme/widgets"


def _event(kind: str, subject: int, **detail: Any) -> None:
    _sql(
        """INSERT INTO public.factory_state_events
           (source, event_type, subject_type, subject_number, detail)
           VALUES ('github-actions', $1, 'worker', $2, $3::jsonb)""",
        kind,
        subject,
        json.dumps({"repo": REPO, **detail}),
    )


def test_the_query_collapses_refusals_and_reads_only_this_repository(neon: str) -> None:  # noqa: F811
    _event("worker_assigned", 3, run_id="70", targets=[5, 1005], claim_id="secret-claim")
    for index in range(150):
        _event(
            "model_attempt",
            3,
            run_id="70",
            selected_number=5,
            outcome_class="model_unavailable",
            started_at=f"2026-11-24T10:00:00.{index:07d}Z",
            failure_log_tail="raw tail",
        )
    _event(
        "model_attempt",
        3,
        run_id="70",
        selected_number=5,
        outcome_class="success",
        duration_ms=90000,
        agent_exit_code=0,
    )
    _event("worker_completed", 3, run_id="70", selected_number=5, outcome_class="success")
    _sql(
        """INSERT INTO public.factory_state_events
           (source, event_type, subject_type, subject_number, detail)
           VALUES ('rotisserie-external', 'external_reserved', 'target', 5, $1::jsonb)""",
        json.dumps({"repo": REPO, "client": "label", "actor": "owner"}),
    )
    _event("model_attempt", 3, run_id="71", selected_number=5, repo="other/repo")

    data = estimate_neon.read_history(REPO, days=2, cap=100, ledger=ledger)
    assert data["status"] == "ok", data
    assert data["rows_read"] == 5 and data["truncated"] is False
    entry = data["numbers"]["5"]  # type: ignore[index]
    assert [run["run_id"] for run in entry["runs"]] == ["70"]
    run = entry["runs"][0]
    assert run["reached_model"] is True and run["refusals"] == 150
    assert entry["counts"] == {"owner_claim": 1}
    text = json.dumps(data)
    for private in ("secret-claim", "raw tail", "owner", "other/repo"):
        assert private not in text.replace("owner_claim", "")


def test_the_row_cap_marks_the_history_truncated(neon: str) -> None:  # noqa: F811
    for run in range(120):
        _event(
            "model_attempt",
            3,
            run_id=str(run),
            selected_number=9,
            outcome_class="success",
            duration_ms=10,
        )
    data = estimate_neon.read_history(REPO, days=2, cap=100, ledger=ledger)
    assert data["truncated"] is True and data["rows_read"] == 100
