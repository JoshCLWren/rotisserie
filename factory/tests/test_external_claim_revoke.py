"""Operator revoke of an external claim is explicit, UUID-fenced and logged."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import factory_state_ledger as ledger  # noqa: E402

REPO = "acme/widgets"
TOKEN = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class _Tx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class KvConnection:
    """Just enough of asyncpg to run the revoke/holder SQL against a dict."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.locked = False

    def transaction(self) -> _Tx:
        return _Tx()

    async def close(self) -> None:
        return None

    async def fetchrow(self, statement: str, key: str) -> dict[str, Any] | None:
        assert "FROM public.factory_state_kv WHERE cache_key=$1" in statement
        if key not in self.rows:
            return None
        return {"payload": json.dumps(self.rows[key]), "expires_at": "2026-11-22T23:00:00+00:00"}

    async def execute(self, statement: str, *args: Any) -> None:
        if "pg_advisory_xact_lock" in statement:
            self.locked = True
            return
        assert self.locked, "every mutation must hold the repository lock"
        if statement.lstrip().startswith("DELETE"):
            keys, token = args
            for key in keys:
                if key in self.rows and self.rows[key][0].get("claim_id") == token:
                    del self.rows[key]
            return
        if "factory_state_events" in statement:
            self.events.append((statement, json.loads(args[-1])))
            return
        raise AssertionError(statement)


def _claim(token: str, targets: list[int], *, external: bool = True) -> list[dict[str, Any]]:
    return [{
        "claim_id": token, "external": external, "actor": "alice",
        "client": "codebuff-phone", "targets": targets, "target": f"pr:{targets[-1]}",
        "action": "review-findings",
    }]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    dsn = tmp_path / "dsn"
    dsn.write_text("postgres://unused")
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", str(dsn))
    holder: dict[str, Any] = {}

    async def connect(**_: Any) -> KvConnection:
        return holder["conn"]

    monkeypatch.setattr(ledger, "measured_connect", connect)
    monkeypatch.setattr(ledger, "_dsn", lambda: "postgres://unused")
    return holder


def _keys(*numbers: int) -> list[str]:
    return [ledger._target_key(REPO, n) for n in numbers]


def test_revoke_removes_only_the_fenced_claim_and_logs_operator(store: dict[str, Any]) -> None:
    newer_issue = ledger._target_key(REPO, 9)
    conn = KvConnection({
        ledger._external_key(REPO, TOKEN): _claim(TOKEN, [7, 8]),
        _keys(7)[0]: _claim(TOKEN, [7, 8]),
        _keys(8)[0]: _claim(TOKEN, [7, 8]),
        newer_issue: _claim(OTHER, [9]),
    })
    store["conn"] = conn
    result = ledger.revoke_external(REPO, "josh", TOKEN, reason="operator take-over of #8")
    assert result["status"] == "revoked"
    assert result["targets"] == [7, 8]
    assert set(conn.rows) == {newer_issue}
    [(statement, detail)] = conn.events
    assert "external_revoked" in statement
    assert detail["operator"] == "josh"
    assert detail["client"] == "codebuff-phone"
    assert detail["reason"] == "operator take-over of #8"


def test_revoke_of_missing_claim_is_a_conflict(store: dict[str, Any]) -> None:
    conn = KvConnection({})
    store["conn"] = conn
    result = ledger.revoke_external(REPO, "josh", TOKEN, reason="cleanup")
    assert result == {"status": "conflict", "reason": "claim_missing_or_replaced"}
    assert conn.events == []


def test_revoke_never_touches_a_factory_worker_claim(store: dict[str, Any]) -> None:
    conn = KvConnection({ledger._external_key(REPO, TOKEN): _claim(TOKEN, [7], external=False)})
    store["conn"] = conn
    assert ledger.revoke_external(REPO, "josh", TOKEN, reason="x")["reason"] == "stale_claim"
    assert ledger._external_key(REPO, TOKEN) in conn.rows


@pytest.mark.parametrize("reason", ["", "  ", "two\nlines", "x" * 501])
def test_revoke_requires_a_one_line_reason(store: dict[str, Any], reason: str) -> None:
    with pytest.raises(ValueError, match="reason"):
        ledger.revoke_external(REPO, "josh", TOKEN, reason=reason)


def test_revoke_rejects_malformed_claim_id(store: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="claim ID"):
        ledger.revoke_external(REPO, "josh", "not-a-uuid", reason="x")


def test_revoke_requires_neon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_STATE_DSN_FILE", raising=False)
    with pytest.raises(RuntimeError, match="Neon"):
        ledger.revoke_external(REPO, "josh", TOKEN, reason="x")


def test_holder_reports_external_claims_only(store: dict[str, Any]) -> None:
    store["conn"] = KvConnection({
        _keys(7)[0]: _claim(TOKEN, [7]),
        _keys(8)[0]: _claim(OTHER, [8], external=False),
    })
    found = ledger.external_holder(REPO, 7)
    assert found is not None
    assert found["claim_id"] == TOKEN
    assert found["client"] == "codebuff-phone"
    assert ledger.external_holder(REPO, 8) is None
    assert ledger.external_holder(REPO, 9) is None


def test_holder_validates_input(store: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ledger.external_holder("not a repo", 7)
    with pytest.raises(ValueError):
        ledger.external_holder(REPO, 0)
