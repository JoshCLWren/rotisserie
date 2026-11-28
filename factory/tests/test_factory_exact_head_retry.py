"""Pre-session abort cooling follows immutable dispatch identity, not PR number."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import factory_libsql as libsql
import factory_turso_core as core
import factory_turso_events as events
import factory_turso_leases as leases
import factory_turso_operations as operations

REPO = "acme/widgets"
HEAD = "a" * 40
NEW = "b" * 40
PR = 701
ISSUE = 700
STAGE = "factory:review"


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = libsql.FileStore(tmp_path / "state.sqlite")
    libsql.ensure_schema(db)
    monkeypatch.setattr(core, "enabled", lambda: True)
    monkeypatch.setattr(core, "store", lambda: db)
    monkeypatch.setenv("FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS", "1800")
    return db


def claim(db, token, *, head=HEAD, stage=STAGE, pr=PR, attest=True):
    db.batch(leases.claim_statements(
        REPO, "18", [ISSUE, pr], 600, token, "nvidia",
        external_wip_seen=None, cap=None,
    ))
    context = {
        "kind": "pr", "number": pr, "worker": "18", "claim_id": token,
        "head": head, "stage": stage,
    }
    if attest:
        assert events.attest_assignment(REPO, context) == context
    return context


def abort(token, reason="pre-session-abort-run-123"):
    assert leases.release_lease(REPO, "18", token, reason)


def test_cooldown_is_exact_head_and_stage_and_never_applies_to_linked_issue(store):
    for token in ("first", "second"):
        claim(store, token)
        abort(token)
    assert operations._review_churn(store, REPO, {PR: (HEAD, STAGE), ISSUE: (HEAD, STAGE)}) == {PR}
    assert operations._review_churn(store, REPO, {PR: (NEW, STAGE)}) == set()
    assert operations._review_churn(store, REPO, {PR: (HEAD, "factory:ci")}) == set()
    assert operations._review_churn(store, "elsewhere/widgets", {PR: (HEAD, STAGE)}) == set()


def test_unscoped_old_releases_and_wrong_stage_do_not_poison_current_head(store):
    for token in ("legacy1", "legacy2"):
        claim(store, token, attest=False)
        abort(token)
    for token in ("other1", "other2"):
        claim(store, token, stage="factory:changes-requested")
        abort(token)
    assert operations._review_churn(store, REPO, {PR: (HEAD, STAGE)}) == set()


def test_replay_does_not_increment_and_scope_cannot_be_rewritten(store):
    context = claim(store, "first")
    assert events.attest_assignment(REPO, context) == context
    with pytest.raises(RuntimeError, match="could not be attested"):
        events.attest_assignment(REPO, {**context, "head": NEW})
    abort("first")
    leases.release_lease(REPO, "18", "first", "pre-session-abort-run-123")
    assert operations._review_churn(store, REPO, {PR: (HEAD, STAGE)}) == set()
    with pytest.raises(RuntimeError, match="could not be attested"):
        events.attest_assignment(REPO, context)


def test_cooldown_expires_and_normal_release_does_not_count(store):
    claim(store, "normal")
    abort("normal", "controller-release")
    claim(store, "first")
    abort("first")
    assert operations._review_churn(store, REPO, {PR: (HEAD, STAGE)}) == set()
    claim(store, "second")
    abort("second")
    assert operations._review_churn(store, REPO, {PR: (HEAD, STAGE)}) == {PR}
    store.batch([(
        "UPDATE factory_state_events SET created_at = unixepoch() - 7200"
        " WHERE event_type = 'worker_released'",
        {},
    )])
    assert operations._review_churn(store, REPO, {PR: (HEAD, STAGE)}) == set()
