"""factory estimate --via github: the bounded Neon query, its summary and the artifact."""

from __future__ import annotations

import io
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rotisserie import cli, web
from rotisserie.factory_console.service import estimate_model as model
from rotisserie.factory_console.service import estimate_neon as neon
from rotisserie.factory_console.service import estimate_outcomes as outcomes
from rotisserie.factory_console.service import estimate_source

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def event(kind: str, minutes: int, **detail: Any) -> dict[str, Any]:
    return {"event_type": kind, "created_at": NOW - timedelta(minutes=minutes), **detail}


def attempt(run: str, number: int, minutes: int, outcome: str, **extra: Any) -> dict[str, Any]:
    started = (NOW - timedelta(minutes=minutes)).isoformat()
    row = {"run_id": run, "selected_number": str(number), "outcome_class": outcome}
    row.update(source="nvidia", model="kimi", started_at=started)
    return event("model_attempt", minutes, **{**row, **extra})


def test_the_query_is_one_bounded_read_of_named_fields() -> None:
    sql, args = neon.history_query("acme/widgets", 30, 500)
    words = sql.upper().split()
    assert words[0] == "WITH" and words.count("SELECT") == 3
    assert not {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT"} & set(words)
    assert "public.factory_state_events" in sql and sql.endswith("LIMIT $4")
    assert "make_interval(days => $2)" in sql and "detail->>'repo' = $1" in sql
    for secret in ("failure_log_tail", "claim_id", "'actor'", "evidence", "SELECT *"):
        assert secret not in sql
    assert args == ("acme/widgets", 30, list(neon.EVENT_TYPES), 501)
    for days, rows in ((0, 500), (121, 500), (30, 99), (30, 100001)):
        with pytest.raises(ValueError):
            neon.history_query("acme/widgets", days, rows)


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.transactions: list[dict[str, object]] = []
        self.closed = False

    def transaction(self, **options: object) -> FakeConnection:
        self.transactions.append(options)
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, sql: str, *args: object) -> None:
        self.calls.append((sql, args))

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self.rows

    async def close(self) -> None:
        self.closed = True


class FakeLedger:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.connection = FakeConnection(rows)

    def _dsn(self) -> str:
        return "postgresql://reader@localhost/factory"

    async def measured_connect(self, **options: object) -> FakeConnection:
        return self.connection


def test_the_read_runs_in_a_read_only_transaction_with_a_timeout() -> None:
    ledger = FakeLedger([attempt("1", 5, 10, "success", duration_ms="900")])
    data = neon.read_history("acme/widgets", days=10, cap=200, now=NOW, ledger=ledger)
    assert data["status"] == "ok" and data["rows_read"] == 1
    conn = ledger.connection
    assert conn.transactions == [{"readonly": True, "isolation": "repeatable_read"}]
    assert conn.calls[0][0] == f"SET LOCAL statement_timeout = {neon.STATEMENT_TIMEOUT_MS}"
    assert conn.calls[1][0] == neon.QUERY and conn.closed


def test_without_a_neon_secret_the_mode_still_publishes_valid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FACTORY_STATE_DSN_FILE", raising=False)
    data = neon.read_history("acme/widgets", now=NOW)
    assert data["status"] == "unavailable" and "FACTORY_STATE_DSN_FILE" in str(data["reason"])
    json.dumps(data)
    with pytest.raises(ValueError, match="unavailable"):
        estimate_source.parse(data)


def test_a_mass_refusal_counts_once_as_refusals_not_attempts() -> None:
    burst = [attempt("7", 5, 50 - n // 100, "model_unavailable") for n in range(300)]
    burst += [attempt("7", 5, 47, "switched") for _ in range(40)]
    rows = [
        event("worker_assigned", 60, targets=[5, 1005], run_id="7"),
        *burst,
        attempt("7", 5, 40, "success", duration_ms="120000", agent_exit_code="0"),
        event("worker_completed", 30, run_id="7", selected_number="5", outcome_class="success"),
        attempt("8", 5, 20, "provider_quota", duration_ms="1500"),
        event("worker_failed", 19, run_id="8", selected_number="5", outcome_class="no_capacity"),
        attempt("9", 5, 10, "agent_timeout", duration_ms="600000"),
        *[attempt("10", 5, 5, "switched") for _ in range(50)],
        event("model_attempt", 4, run_id="11", selected_number="5", row_count=250)
        | {"outcome_class": "model_unavailable"},
    ]
    data = neon.summarize(list(reversed(rows)), days=30, cap=1000, now=NOW)
    history = estimate_source.parse(data)
    runs = history.runs((5, 1005))
    assert (runs.attempts, runs.capacity_runs, runs.refusals) == (2, 3, 641)
    assert runs.assignments == 1
    assert data["unknown_classes"] == {}


def test_a_run_with_no_model_rows_counts_by_its_terminal_class() -> None:
    rows = [
        event("worker_failed", 5, run_id="3", selected_number="6", outcome_class="work_failure"),
        event("worker_failed", 4, run_id="4", selected_number="6", outcome_class="no_capacity"),
        event("worker_idle", 3, run_id="5", selected_number="6", outcome_class="no_work"),
    ]
    history = estimate_source.parse(neon.summarize(rows, days=30, cap=100, now=NOW))
    assert history.runs((6,)).attempts == 1


def test_outcome_classes_map_explicitly_and_unknown_ones_count_as_attempts() -> None:
    assert outcomes.role("no_capacity") == outcomes.CAPACITY
    assert outcomes.role("provider_quota") == outcomes.CAPACITY
    assert outcomes.role("provider_throttle") == outcomes.CAPACITY
    assert outcomes.role("no_work") == outcomes.IDLE
    assert outcomes.role("work_failure") == outcomes.ATTEMPT
    assert outcomes.role("brand_new_class") == outcomes.ATTEMPT
    assert outcomes.role(None) == outcomes.ATTEMPT
    rows = [attempt("2", 9, 5, "brand_new_class", duration_ms="10")]
    data = neon.summarize(rows, days=30, cap=100, now=NOW)
    assert data["unknown_classes"] == {"brand_new_class": 1}
    assert estimate_source.parse(data).runs((9,)).attempts == 1


def test_owner_label_claims_are_people_and_outside_agents_are_not() -> None:
    rows = [
        event("external_reserved", 9, subject_number=4, client="label"),
        event("external_reserved", 8, subject_number=4, client="codex"),
        event("external_revoked", 7, subject_number=4),
    ]
    data = neon.summarize(rows, days=30, cap=100, now=NOW)
    entry = data["numbers"]["4"]  # type: ignore[index]
    assert entry["counts"] == {"owner_claim": 1, "outside_claims": 1, "operator_revoked": 1}
    assert estimate_source.parse(data).runs((4,)).signals == ("owner_claim", "operator_revoked")
    assert "codex" not in json.dumps(data)


def test_a_row_cap_moves_the_window_start_to_the_oldest_row_read() -> None:
    rows = [attempt(str(n), 3, n, "success", duration_ms="5") for n in range(1, 12)]
    data = neon.summarize(rows, days=30, cap=10, now=NOW)
    assert data["truncated"] is True and data["rows_read"] == 10
    history = estimate_source.parse(data)
    assert history.covered_from == NOW - timedelta(minutes=10)
    assert not history.covers(NOW - timedelta(hours=1)) and history.covers(NOW)


def test_artifacts_from_another_mode_or_schema_are_refused() -> None:
    good = neon.summarize([], days=30, cap=100, now=NOW)
    assert estimate_source.load(good)[0] is not None
    bad_artifacts: list[dict[str, object]] = [
        {**good, "schema": 99},
        {**good, "mode": "leases"},
        {**good, "covered_from": "yesterday"},
        {"result": "pending"},
    ]
    for bad in bad_artifacts:
        history, why = estimate_source.load(bad)
        assert history is None and why


def test_coverage_starts_at_the_first_claim_and_person_signals_always_apply() -> None:
    rows = [
        event("external_reserved", 5, subject_number=8, client="label"),
        attempt("40", 8, 4, "success", duration_ms="9000"),
    ]
    history = estimate_source.parse(neon.summarize(rows, days=3, cap=100, now=NOW))
    backlog = model.Item(
        "issue:8",
        (8,),
        model.Features("issue", "normal", (), "small", 0),
        "merged",
        3,
        NOW - timedelta(days=1),
        None,
        NOW,
        NOW,
        (),
        created_at=NOW - timedelta(days=60),
    )
    found, runs = estimate_source.apply(backlog, history)
    assert runs is not None and found.attempts == 1 and found.signals == ("owner_claim",)
    old = replace(backlog, first_claim=NOW - timedelta(days=10))
    found, runs = estimate_source.apply(old, history)
    assert runs is None and found.attempts == 3 and found.signals == ("owner_claim",)


def test_the_label_claim_workflow_runs_only_the_bounded_history_command() -> None:
    workflow = (ROOT / ".github/workflows/rotisserie-label-claim.yml").read_text()
    assert "estimate-history) args=(factory estimate-history) ;;" in workflow
    caller = (ROOT / "factory/install/callers/rotisserie-label-claim.yml").read_text()
    assert "estimate-history]" in caller


def test_the_web_ui_starts_and_reads_a_neon_estimate() -> None:
    assert web.write_command({"operation": "estimate_neon", "target": "issue:5"}) == [
        "estimate",
        "issue:5",
        "--via",
        "github",
        "--no-wait",
    ]
    assert web.read_command("estimate", {"target": "pr:9", "run": "123"}) == [
        "estimate",
        "pr:9",
        "--run",
        "123",
    ]
    for bad in ({"target": "pr:9", "run": "12; rm"}, {"target": "pr:9", "run": "0"}):
        with pytest.raises(web.InvalidRequest):
            web.read_command("estimate", bad)
    with pytest.raises(web.InvalidRequest):
        web.write_command({"operation": "estimate_neon", "target": "issue:5 --via direct"})
    page = (ROOT / "src/rotisserie/web_ui.html").read_text()
    assert 'id="item-estimate-neon"' in page and 'data-item-read="estimate"' in page


def run_cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, dict[str, Any]]:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    code = cli.main(["factory", "--repo", "acme/widgets", "--json", *args])
    return code, json.loads(stream.getvalue().strip().splitlines()[-1])


def test_a_pending_run_says_so_and_no_wait_returns_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(estimate_source, "earlier", lambda github, run: {"result": "pending"})
    code, data = run_cli(monkeypatch, "estimate", "issue:5", "--run", "77")
    assert data["result"] == "pending" and code == 6
    started = {"status": "started", "bridge": {"run_id": 88, "url": "https://github.com/x"}}
    monkeypatch.setattr(estimate_source, "start", lambda github: started)
    code, data = run_cli(monkeypatch, "estimate", "issue:5", "--via", "github", "--no-wait")
    assert code == 0 and data["bridge"]["run_id"] == 88 and data["result"] == "submitted"
    code, data = run_cli(monkeypatch, "estimate", "issue:5", "--no-wait")
    assert code != 0


def test_estimate_history_refuses_to_run_outside_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    code, data = run_cli(monkeypatch, "estimate-history")
    assert code == 4 and data["status"] == "refused"
