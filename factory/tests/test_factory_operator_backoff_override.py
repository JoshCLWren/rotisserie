"""Trusted operator exact-model backoff override keeps hard provider protections."""

from __future__ import annotations

import importlib

import pytest

from tests import turso_support as support

ops = importlib.import_module("factory_state_operations")

WORKER = "1327811785100"
MODEL = "hy4-flash-free"
RUNTIME = f"opencode-go/{MODEL}"
SOURCE = "opencode-free"


@pytest.fixture
def store(tmp_path, monkeypatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        support.use_roster(monkeypatch, {WORKER: (SOURCE, "")})
        yield db, repo


def seed(
    store,
    status="healthy",
    outcome="work_failure",
    backoff_outcome="work_failure",
    suspended=False,
    quota=False,
):
    db, repo = store
    until = 86400 if status == "cooling" else None
    support.health(db, repo, SOURCE, MODEL, status, until, outcome=outcome)
    support.backoff(
        db,
        repo,
        "model",
        source=SOURCE,
        model=MODEL,
        outcome=backoff_outcome,
        streak=3,
        retry_in=86400,
        detail={"suspended": suspended},
    )
    if quota:
        support.backoff(db, repo, "provider", source=SOURCE, outcome="provider_quota")


def export(store, requested_model="auto", worker=WORKER):
    _, repo = store
    return ops.export_model_exclusions(repo, worker, requested_model)


def test_automatic_fleet_keeps_existing_work_failure_backoff(store):
    seed(store)
    assert MODEL in export(store).get(SOURCE, [])


def test_operator_can_retry_one_live_free_go_model_after_failed_coding_work(store):
    seed(store)
    assert MODEL not in export(store, RUNTIME).get(SOURCE, [])


@pytest.mark.parametrize(
    "status,outcome,backoff_outcome,suspended,quota",
    [
        ("retired", "model_retired_410", "work_failure", False, False),
        ("cooling", "provider_failure", "work_failure", False, False),
        ("healthy", "work_failure", "agent_timeout", False, False),
        ("healthy", "work_failure", "work_failure", True, False),
        ("healthy", "provider_failure", "work_failure", False, False),
        ("healthy", "work_failure", "work_failure", False, True),
    ],
)
def test_exact_pin_cannot_bypass_hard_unavailability(
    store,
    status,
    outcome,
    backoff_outcome,
    suspended,
    quota,
):
    seed(store, status, outcome, backoff_outcome, suspended, quota)
    assert MODEL in export(store, RUNTIME).get(SOURCE, [])


def test_operator_override_requires_trusted_matching_worker(store):
    seed(store)
    with pytest.raises(ValueError, match="trusted worker"):
        export(store, RUNTIME, worker="")
    with pytest.raises(ValueError, match="does not belong"):
        export(store, "openrouter/not-the-go-model")
