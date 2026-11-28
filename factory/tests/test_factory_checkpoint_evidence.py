"""Saved-work evidence does not rewrite execution or credit resumed code."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import factory_libsql as libsql
import factory_turso_core as core
import factory_turso_events as events
import factory_turso_runtime as runtime

from tests.test_factory_checkpoint_handoff import harness, handoff, REPO, RUN, HEAD


def test_resumed_branch_evidence_uses_source_commit(harness):
    harness["saved"]["origin_head"] = HEAD
    harness["saved"]["head"] = "d" * 40
    harness["saved"]["resumed"] = True
    assert handoff.publish(REPO, RUN)["checkpoint_sha"] == HEAD


def test_publication_failure_does_not_credit_wrong_worker(harness, monkeypatch, tmp_path):
    harness["saved"]["worker"] = "77"
    path = tmp_path / "outcome.json"
    path.write_text(json.dumps({"outcome_class": "work_failure"}))
    monkeypatch.setattr(sys, "argv", ["handoff", "--repo", REPO, "--run-id", RUN,
                                     "--outcome", str(path)])
    assert handoff.main() == 1
    assert json.loads(path.read_text())["checkpoint_saved"] is False


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = libsql.FileStore(tmp_path / "ledger.sqlite")
    libsql.ensure_schema(db)
    monkeypatch.setattr(core, "enabled", lambda: True)
    monkeypatch.setattr(core, "store", lambda: db)
    monkeypatch.setattr(runtime, "project_runtime", lambda *_: False)
    return db


def test_late_verified_milestones_do_not_replace_failed_terminal_event(store):
    failure = {"worker": "46", "outcome_class": "agent_timeout", "persisted_work": False,
               "delivery_outcome": "none", "checkpoint_saved": True, "checkpoint_sha": HEAD}
    assert events.record_runtime_outcome(REPO, RUN, failure) == 2
    assert events.record_runtime_outcome(REPO, RUN, failure) == 0
    published = {**failure, "checkpoint_published": True, "work_pr": 5152}
    assert events.record_runtime_outcome(REPO, RUN, published) == 1
    validated = {**published, "checkpoint_validated": True}
    assert events.record_runtime_outcome(REPO, RUN, validated) == 1
    assert events.record_runtime_outcome(REPO, RUN, validated) == 0
    rows = store.read("SELECT event_type, detail FROM factory_state_events WHERE repo = :repo",
                      {"repo": REPO})
    terminals = [json.loads(r["detail"]) for r in rows if r["event_type"] == "worker_failed"]
    assert len(terminals) == 1
    assert terminals[0]["outcome_class"] == "agent_timeout"
    assert terminals[0]["delivery_outcome"] == "none"
    assert not terminals[0]["persisted_work"]
    evidence = [json.loads(r["detail"]) for r in rows if r["event_type"] == "checkpoint_evidence"]
    assert {e["milestone"] for e in evidence} == {"saved", "published", "validated"}
    assert all(e["checkpoint_sha"] == HEAD and e["run_id"] == RUN for e in evidence)
    assert {e["work_pr"] for e in evidence} == {None, 5152}


@pytest.mark.parametrize("fields", [
    {"checkpoint_saved": False, "checkpoint_sha": HEAD, "work_pr": 5152},
    {"checkpoint_saved": True, "checkpoint_sha": "invalid", "work_pr": 5152},
    {"checkpoint_saved": "true", "checkpoint_sha": HEAD},
    {"work_pr": 5152, "delivery_outcome": "persisted"},
])
def test_missing_source_proof_does_not_create_evidence(fields):
    assert events.checkpoint_evidence(RUN, "46", fields) == []


def test_validation_requires_published_pr_and_source_commit():
    base = {"checkpoint_saved": True, "checkpoint_sha": HEAD, "checkpoint_validated": True}
    assert [e["milestone"] for e in events.checkpoint_evidence(RUN, "46", base)] == ["saved"]
    assert [e["milestone"] for e in events.checkpoint_evidence(
        RUN, "46", {**base, "checkpoint_published": True, "work_pr": 0}
    )] == ["saved"]
