"""Operator revoke of an external claim is explicit, UUID-fenced and logged."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests import turso_support as support

ledger = importlib.import_module("factory_state_ledger")
TOKEN = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def _reserve(repo: str, numbers: list[int]) -> dict[str, Any]:
    claim = ledger.reserve_external(
        repo,
        "alice",
        "codebuff-phone",
        numbers,
        target=f"pr:{numbers[-1]}",
        action="review-findings",
        head=None,
        observed_wip=0,
        wip_limit=8,
        offer_at="2026-11-22T00:00:00+00:00",
    )
    assert claim["status"] == "reserved"
    return claim


def _keys(db: Any, repo: str) -> set[str]:
    rows = db.read("SELECT lease_key FROM factory_lease WHERE repo = :r", {"r": repo})
    return {str(r["lease_key"]) for r in rows}


def test_revoke_removes_only_the_fenced_claim_and_logs_operator(env) -> None:
    db, repo = env
    token = _reserve(repo, [7, 8])["claim_id"]
    newer = _reserve(repo, [9])["claim_id"]
    result = ledger.revoke_external(repo, "josh", token, reason="operator take-over of #8")
    assert result["status"] == "revoked"
    assert result["targets"] == [7, 8]
    assert _keys(db, repo) == {ledger._external_key(repo, newer), ledger._target_key(repo, 9)}
    (row,) = db.read(
        "SELECT detail FROM factory_state_events WHERE repo = :r"
        " AND event_type = 'external_revoked'",
        {"r": repo},
    )
    detail = json.loads(row["detail"])
    assert detail["operator"] == "josh"
    assert detail["client"] == "codebuff-phone"
    assert detail["reason"] == "operator take-over of #8"


def test_revoke_of_missing_claim_is_a_conflict(env) -> None:
    db, repo = env
    result = ledger.revoke_external(repo, "josh", TOKEN, reason="cleanup")
    assert result == {"status": "conflict", "reason": "claim_missing_or_replaced"}
    events = "SELECT count(*) AS n FROM factory_state_events WHERE repo = :r"
    assert support.count(db, events, r=repo) == 0


def test_revoke_never_touches_a_factory_worker_claim(env) -> None:
    _, repo = env
    worker = support.uncapped_workers(1)[0]
    token = ledger.acquire_lease(repo, worker, [7])
    assert token
    refused = ledger.revoke_external(repo, "josh", token, reason="x")
    assert refused == {"status": "conflict", "reason": "claim_missing_or_replaced"}
    assert ledger.worker_claim(repo, worker) == (token, [7])


@pytest.mark.parametrize("reason", ["", "  ", "two\nlines", "x" * 501])
def test_revoke_requires_a_one_line_reason(env, reason: str) -> None:
    _, repo = env
    with pytest.raises(ValueError, match="reason"):
        ledger.revoke_external(repo, "josh", TOKEN, reason=reason)


def test_revoke_rejects_malformed_claim_id(env) -> None:
    _, repo = env
    with pytest.raises(ValueError, match="claim ID"):
        ledger.revoke_external(repo, "josh", "not-a-uuid", reason="x")


def test_revoke_requires_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    with pytest.raises(RuntimeError, match="factory store"):
        ledger.revoke_external("acme/widgets", "josh", TOKEN, reason="x")


def test_holder_reports_external_claims_only(env) -> None:
    _, repo = env
    token = _reserve(repo, [7])["claim_id"]
    assert ledger.acquire_lease(repo, support.uncapped_workers(1)[0], [8])
    found = ledger.external_holder(repo, 7)
    assert found is not None
    assert found["claim_id"] == token
    assert found["client"] == "codebuff-phone"
    assert ledger.external_holder(repo, 8) is None
    assert ledger.external_holder(repo, 9) is None


def test_holder_validates_input(env) -> None:
    _, repo = env
    with pytest.raises(ValueError):
        ledger.external_holder("not a repo", 7)
    with pytest.raises(ValueError):
        ledger.external_holder(repo, 0)
