"""Turso usage: API reads, projection, status, the pause helper, the CLI and both UIs."""

from __future__ import annotations

import argparse
import base64
import io
import json
import urllib.error
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rotisserie import control_contract as contract
from rotisserie.control_parity import CONTROLS
from rotisserie.factory_console import commands, runtime
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import db_usage
from rotisserie.factory_console.tracker import Clock

usage = runtime.module("factory_turso_usage")
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 16, tzinfo=UTC)
URL = "https://rotisserie-factory-acme.aws-us-east-1.turso.io"
ENV = {"FACTORY_TURSO_API_TOKEN": "secret-token", "FACTORY_TURSO_URL": URL}
FREE = {"rowsRead": 500_000_000, "rowsWritten": 10_000_000}
FREE.update({"storage": 5_000_000_000, "bytesSynced": 3_000_000_000})


def counts(reads: int = 0, writes: int = 0, storage: int = 4096, synced: int = 0) -> dict[str, int]:
    return {
        "rows_read": reads,
        "rows_written": writes,
        "storage_bytes": storage,
        "bytes_synced": synced,
    }


def fake_api(
    own: dict[str, int],
    others: Sequence[dict[str, int]] = (),
    *,
    blocked: bool = False,
    plans: object = None,
    overages: bool = False,
) -> tuple[Any, list[str]]:
    calls: list[str] = []
    org = {"plan_id": "free", "blocked_reads": blocked, "blocked_writes": False}
    org["overages"] = overages
    answers: dict[str, object] = {
        "/organizations/acme": {"organization": org},
        "/organizations/acme/usage": {
            "organization": {"usage": counts(1, 1, 1, 1)},
            "databases": [{"total": own}, *({"total": other} for other in others)],
        },
        "/organizations/acme/databases/rotisserie-factory/usage": {"database": {"total": own}},
        "/organizations/acme/plans": plans
        if plans is not None
        else {"plans": [{"name": "free", "quotas": FREE}]},
        "/organizations/acme/databases/rotisserie-factory/stats": {"top_queries": []},
    }

    def fetch(path: str, what: str) -> object:
        calls.append(path)
        return answers[path]

    return fetch, calls


def read(fetch: Any, now: datetime = NOW, env: dict[str, str] = ENV) -> dict[str, Any]:
    result: dict[str, Any] = usage.from_env(env, now=now, fetch=fetch)
    return result


def test_an_empty_database_is_ok_and_in_use() -> None:
    fetch, calls = fake_api(counts())
    report = read(fetch)
    assert report["status"] == "ok"
    assert report["label"] == "" and report["in_use"] is True
    assert report["allowance_source"] == "turso plan free"
    assert report["metrics"]["rows_read"]["allowance"] == 500_000_000
    assert report["resets_at"] == "2026-12-01T00:00:00+00:00"
    assert report["reset_hour_verified"] is False
    assert report["days_left"] == 15.0
    assert calls[0] == "/organizations/acme"
    assert "secret-token" not in json.dumps(report) and "acme" not in json.dumps(report)


def test_without_a_store_url_the_store_is_not_in_use() -> None:
    fetch, _ = fake_api(counts())
    report = read(fetch, env={**ENV, "FACTORY_TURSO_URL": ""})
    assert report["in_use"] is False and report["label"] == "not in use yet"


def test_projection_over_seventy_percent_warns() -> None:
    fetch, _ = fake_api(counts(), [counts(reads=200_000_000)])
    report = read(fetch)
    metric = report["metrics"]["rows_read"]
    assert metric["share_used"] == 0.4
    assert metric["projected_organization"] == 400_000_000
    assert report["status"] == "warn"
    assert "rows_read projected at 80% of the allowance" in report["reasons"]


def test_owner_targets_warn_for_the_factory_database() -> None:
    fetch, _ = fake_api(counts(writes=600_000))
    report = read(fetch)
    assert report["status"] == "warn"
    assert report["reasons"] == ["rows_written projected over the owner target 1,000,000"]
    fetch, _ = fake_api(counts(storage=300_000_000))
    assert read(fetch)["reasons"] == ["storage_bytes projected over the owner target 250,000,000"]


def test_over_ninety_percent_used_is_critical_and_pauses() -> None:
    fetch, _ = fake_api(counts(), [counts(writes=9_500_000)])
    report = read(fetch, now=NOW.replace(day=28))
    assert report["status"] == "critical"
    assert report["reasons"][0] == "rows_written at 95% of the allowance"
    pause, why = usage.pause_new_claims(report)
    assert pause and "critical" in why


def test_blocked_is_critical_even_with_little_usage() -> None:
    fetch, _ = fake_api(counts(), blocked=True)
    report = read(fetch)
    assert report["status"] == "critical"
    assert report["blocked"] == {"reads": True, "writes": False}
    assert report["reasons"][0] == "Turso reports BLOCKED reads"


def test_enabled_overages_warn() -> None:
    fetch, _ = fake_api(counts(), overages=True)
    report = read(fetch)
    assert report["status"] == "warn"
    assert report["reasons"] == ["overages are enabled; the factory budget is $0"]


def test_other_documented_shapes_are_read() -> None:
    fetch, _ = fake_api(counts(reads=5))

    def nested(path: str, what: str) -> object:
        answer: Any = fetch(path, what)
        if path.endswith("/acme/usage"):
            listed = [{"usage": entry["total"]} for entry in answer["databases"]]
            return {"organization": {"usage": {}, "databases": listed}}
        return answer

    report = read(nested)
    assert report["metrics"]["rows_read"]["organization"] == 5
    assert report["organization_source"] == "sum of database totals"


def test_an_unexpected_shape_is_an_error_not_zero_usage() -> None:
    fetch, _ = fake_api(counts())

    def missing_total(path: str, what: str) -> object:
        if path.endswith("rotisserie-factory/usage"):
            return {"database": {"instances": []}}
        return fetch(path, what)

    report = read(missing_total)
    assert report["status"] == "error"
    assert report["reasons"] == ["Turso usage has an unexpected response shape"]

    def not_an_object(path: str, what: str) -> object:
        return [] if path.endswith("/acme/usage") else fetch(path, what)

    assert read(not_an_object)["status"] == "error"


def test_blocked_stays_critical_when_usage_cannot_be_parsed() -> None:
    fetch, _ = fake_api(counts(), blocked=True)

    def broken(path: str, what: str) -> object:
        return {} if path.endswith("rotisserie-factory/usage") else fetch(path, what)

    report = read(broken)
    assert report["status"] == "critical"
    assert report["reasons"][0] == "Turso reports BLOCKED"
    assert usage.pause_new_claims(report)[0]


def test_missing_plan_quotas_fall_back_to_the_documented_free_plan() -> None:
    fetch, _ = fake_api(counts(), plans={"plans": []})
    report = read(fetch)
    assert report["allowance_source"] == "documented free plan"
    assert report["metrics"]["bytes_synced"]["allowance"] == 3_000_000_000


def test_projection_math() -> None:
    window = usage.month_window(datetime(2026, 11, 1, 6, tzinfo=UTC))
    assert window["days_in_month"] == 30.0
    projected = usage.project(counts(reads=1_000, storage=500), window)
    assert projected["rows_read"] == 30_000
    assert projected["storage_bytes"] == 500
    mid = usage.month_window(datetime(2026, 11, 11, tzinfo=UTC))
    assert usage.project(counts(writes=10, synced=20), mid)["rows_written"] == 30
    assert usage.project(counts(writes=10, synced=20), mid)["bytes_synced"] == 60
    december = usage.month_window(datetime(2026, 12, 31, 12, tzinfo=UTC))
    assert december["resets_at"] == "2027-01-01T00:00:00+00:00"
    assert december["days_left"] == 0.5


def test_missing_token_says_not_configured() -> None:
    report = usage.from_env({"FACTORY_TURSO_URL": URL}, now=NOW)
    assert report["status"] == "not_configured"
    assert report["reasons"] == ["not configured: FACTORY_TURSO_API_TOKEN missing"]
    assert usage.pause_new_claims(report) == (False, "")


def test_database_url_parsing_and_overrides() -> None:
    assert usage.parse_database_url(URL) == ("acme", "rotisserie-factory")
    assert usage.parse_database_url("libsql://a-b-team-x.turso.io", org="team-x") == (
        "team-x",
        "a-b",
    )
    assert usage.parse_database_url("libsql://a-b-c.turso.io", database="a") == ("b-c", "a")
    with pytest.raises(usage.UsageError):
        usage.parse_database_url("https://example.com")


def test_http_errors_never_carry_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: Any, timeout: float) -> object:
        assert request.get_header("Authorization") == "Bearer secret-token"
        raise urllib.error.HTTPError(request.full_url, 404, "nope", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(usage.urllib.request, "urlopen", refuse)
    report = usage.from_env(ENV, now=NOW)
    assert report["status"] == "error"
    assert report["reasons"] == [
        "Turso organization answered HTTP 404 (check FACTORY_TURSO_ORG / FACTORY_TURSO_DATABASE)"
    ]
    assert "secret-token" not in json.dumps(report)


def test_pause_helper_for_blocked_answers_and_advisory_states() -> None:
    assert usage.pause_new_claims(error=RuntimeError("SQLite error: BLOCKED"))[0]
    assert not usage.pause_new_claims(error=RuntimeError("database is locked"))[0]
    for state in ("ok", "warn", "error", "not_configured", "unpublished"):
        assert usage.pause_new_claims({"status": state}) == (False, "")
    assert usage.pause_new_claims(None) == (False, "")


def stats_with(report: dict[str, Any] | None) -> dict[str, Any]:
    stats: dict[str, Any] = {"generated_at": NOW.isoformat(), "models": []}
    if report is not None:
        stats["db_usage"] = report
    return stats


def test_doctor_line_reads_only_the_published_value() -> None:
    fetch, _ = fake_api(counts(), [counts(writes=9_500_000)])
    critical = read(fetch)
    status, text, hint = db_usage.health(stats_with(critical), NOW)
    assert status == "fail" and "rows written" in text and "new claims pause" in hint
    fetch, _ = fake_api(counts())
    status, text, _ = db_usage.health(stats_with(read(fetch)), NOW + timedelta(minutes=10))
    assert status == "ok" and text.startswith("rows read 0 of 500.0M")
    status, text, _ = db_usage.health(stats_with(read(fetch)), NOW + timedelta(hours=2))
    assert status == "warn" and text.startswith("published Turso usage is stale")
    assert db_usage.health(stats_with(None), NOW)[0] == "ok"
    assert db_usage.health(None, NOW)[1] == "no Turso usage published yet"


def run_cli(
    monkeypatch: pytest.MonkeyPatch, argv: Sequence[str], stats: dict[str, Any]
) -> tuple[int, str, list[str]]:
    calls: list[str] = []

    def github_run(self: GitHub, args: Sequence[str], **_: object) -> str:
        calls.append(args[1])
        blob = base64.b64encode(json.dumps(stats).encode()).decode()
        return json.dumps({"content": blob})

    monkeypatch.setattr(GitHub, "_run", github_run)
    parser = argparse.ArgumentParser()
    commands.add_parser(parser.add_subparsers(dest="command"))
    stdout = io.StringIO()
    code = commands.main(
        parser.parse_args(["factory", *argv]),
        env={"FACTORY_REPO": "acme/widgets"},
        stdout=stdout,
        clock=Clock(now=lambda: NOW),
    )
    return code, stdout.getvalue(), calls


def test_cli_shows_the_published_value_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch, _ = fake_api(counts(), [counts(reads=200_000_000)])
    code, out, calls = run_cli(monkeypatch, ["db-usage", "--json"], stats_with(read(fetch)))
    assert code == 0
    assert calls == ["repos/acme/widgets/contents/factory-stats.json?ref=factory-status-live"]
    data = json.loads(out)
    assert data["status"] == "warn" and data["source"] == "published"
    assert data["metrics"]["rows_read"]["projected_share"] == 0.8


def test_cli_text_and_live_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch, _ = fake_api(counts())
    code, out, _ = run_cli(monkeypatch, ["db-usage"], stats_with(read(fetch)))
    assert code == 0 and "Turso usage: ok" in out and "not in use" not in out and "rows read" in out
    monkeypatch.delenv("FACTORY_TURSO_API_TOKEN", raising=False)
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    code, out, calls = run_cli(monkeypatch, ["db-usage", "--live", "--json"], {})
    assert code == 0 and calls == []
    assert json.loads(out)["status"] == "not_configured"


def test_both_control_rooms_serve_db_usage_without_sign_in() -> None:
    assert contract.read_command("db-usage", {}) == ["db-usage"]
    assert "db-usage" in contract.PUBLIC_READS and "db-usage" not in contract.LIVE_READS
    assert CONTROLS["db-usage"] == ("read:db-usage",)
    ui = "".join(
        (ROOT / "src/rotisserie" / name).read_text() for name in ("web_ui.html", "web_ui.js")
    )
    assert 'get("db-usage")' in ui and 'id="db-usage-read"' in ui


def test_workflows_publish_usage_and_scope_the_token() -> None:
    pages = (ROOT / ".github/workflows/factory-status-pages.yml").read_text(encoding="utf-8")
    assert "python3 .github/scripts/factory_turso_usage.py" in pages
    assert ".db_usage = $usage[0]" in pages
    claim = (ROOT / ".github/workflows/rotisserie-label-claim.yml").read_text(encoding="utf-8")
    assert "db-usage) args=(factory db-usage --live) ;;" in claim
    assert "inputs.mode == 'db-usage' && secrets.FACTORY_TURSO_API_TOKEN" in claim
    for caller in ("factory-status-pages.yml", "rotisserie-label-claim.yml"):
        text = (ROOT / "factory/install/callers" / caller).read_text(encoding="utf-8")
        assert "FACTORY_TURSO_API_TOKEN: ${{ secrets.FACTORY_TURSO_API_TOKEN }}" in text


def test_top_queries_are_published_scrubbed_and_a_stats_failure_never_fails_the_report() -> None:
    fetch, _calls = fake_api(counts(10, 2, 3, 0))
    stats = "/organizations/acme/databases/rotisserie-factory/stats"
    heavy = {
        "query": "SELECT * FROM factory_state_events WHERE repo = 'acme/widgets' AND n > 42",
        "rows_read": 900,
        "rows_written": 0,
    }
    light = {"query": "SELECT 1", "rows_read": 5, "rows_written": 1}

    def answering(path: str, what: str) -> object:
        return {"top_queries": [light, heavy]} if path == stats else fetch(path, what)

    report = read(answering)
    assert report["top_queries"] == [
        {
            "query": "SELECT * FROM factory_state_events WHERE repo = ? AND n > ?",
            "rows_read": 900,
            "rows_written": 0,
        },
        {"query": "SELECT ?", "rows_read": 5, "rows_written": 1},
    ]

    def refusing(path: str, what: str) -> object:
        if path == stats:
            raise usage.UsageError("Turso database stats answered HTTP 403")
        return fetch(path, what)

    report = read(refusing)
    assert report["status"] in ("ok", "warn", "critical") and report["top_queries"] == []
    assert report["top_queries_error"] == "Turso database stats answered HTTP 403"
