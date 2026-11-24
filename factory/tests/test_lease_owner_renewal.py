"""A claim is renewed only by its own run, and released once that run has completed.

Nov 24, 2026: workers kept worker IDs across runs. Renewing by worker ID alone
could extend a claim whose own Entry run had finished while a later run of the
same worker ran.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests import turso_support as support

leases = importlib.import_module("factory_turso_leases")
events = importlib.import_module("factory_turso_events")
ledger = importlib.import_module("factory_state_ledger")
owners = importlib.import_module("factory_lease_owners")
TARGET = 5756
R1, R2 = "45470929112", "45500999999"


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def iso(offset: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(run_id: str, worker: str, status: str, created: str) -> dict[str, object]:
    return {
        "id": int(run_id),
        "display_title": f"Factory {worker} · auto · fixed-model entry",
        "status": status,
        "created_at": created,
    }


class Live:
    def __init__(self, *runs: dict[str, object]) -> None:
        self.live_runs = runs
        self.run_workers: dict[str, str] = {}


def getter(*runs: dict[str, object]):
    calls: list[str] = []

    def get(path: str) -> object:
        calls.append(path)
        if path.startswith("actions/runs/"):
            wanted = path.rsplit("/", 1)[-1]
            return next((r for r in runs if str(r["id"]) == wanted), {})
        return {"workflow_runs": [r for r in runs if "entry.yml" in path]}

    get.calls = calls  # type: ignore[attr-defined]
    return get


def short_claim(db, repo: str, worker: str) -> str:
    token = leases.acquire_lease(repo, worker, [TARGET], ttl=120)
    assert token
    return token


def started_by_r1(db, repo: str, worker: str, token: str) -> None:
    events.record_lifecycle(repo, R1, worker, "started", token)
    db.batch(
        [
            (
                "UPDATE factory_lease SET expires_at = unixepoch() + 120 WHERE claim_id = :c",
                {"c": token},
            )
        ]
    )


def left(db, token: str) -> int:
    return int(
        db.read(
            "SELECT expires_at - unixepoch() AS left FROM factory_lease"
            " WHERE claim_id = :c AND kind = 'worker'",
            {"c": token},
        )[0]["left"]
    )


def held(db, token: str) -> bool:
    return bool(db.read("SELECT 1 FROM factory_lease WHERE claim_id = :c", {"c": token}))


def test_worker_start_records_its_run_on_the_claim(env) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    token = short_claim(db, repo, worker)
    assert events.record_lifecycle(repo, R1, worker, "started", token) == 1
    (row,) = [r for r in leases.list_leases(repo) if r["key"].endswith(f":worker:{worker}")]
    assert row["run_id"] == R1 and row["acquired_at"]
    assert left(db, token) > 600


def test_finished_r1_claim_is_not_renewed_while_r2_of_the_same_worker_runs(env) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    token = short_claim(db, repo, worker)
    started_by_r1(db, repo, worker, token)
    r1 = run(R1, worker, "completed", iso(-3600))
    r2 = run(R2, worker, "in_progress", iso(3600))
    result = owners.reconcile(repo, getter(r1, r2), Live(r2), ledger)
    assert [(o.run_id, o.state) for o in result] == [(R1, "completed")]
    assert not held(db, token)


def test_finished_r1_claim_is_released_when_no_run_is_active(env) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    token = short_claim(db, repo, worker)
    events.record_lifecycle(repo, R1, worker, "started", token)
    get = getter(run(R1, worker, "completed", iso(-60)))
    owners.reconcile(repo, get, Live(), ledger)
    assert get.calls == [f"actions/runs/{R1}"]
    assert not held(db, token)


def test_unrecorded_claim_ignores_a_later_run_of_the_same_worker(env) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    token = short_claim(db, repo, worker)
    later = run(R2, worker, "in_progress", iso(2 * owners.DISPATCH_WINDOW_SECONDS))
    result = owners.reconcile(repo, getter(), Live(later), ledger)
    assert [o.state for o in result] == ["unknown"]
    assert held(db, token) and left(db, token) <= 120


def test_unrecorded_claim_is_never_released_by_an_inferred_owner(env) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    token = short_claim(db, repo, worker)
    for created in (iso(5), iso(-60)):
        result = owners.reconcile(
            repo, getter(run(R1, worker, "completed", created)), Live(), ledger
        )
        assert [o.state for o in result] == ["unknown"]
        assert held(db, token) and left(db, token) <= 120
    assert (
        owners.claim_owners(leases.list_leases(repo), getter(run(R1, worker, "completed", iso(5))))[
            0
        ].describe()
        == f"run {R1} (dispatched), completed"
    )


def test_live_fork_repair_run_keeps_a_claim_whose_entry_run_finished(env) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    token = short_claim(db, repo, worker)
    started_by_r1(db, repo, worker, token)
    fork = run(R2, worker, "in_progress", iso(1800))
    fork["display_title"] = f"Factory {worker} · fork repair · PR 9 · abc"
    get = getter(run(R1, worker, "completed", iso(-60)))
    owners.reconcile(repo, get, Live(fork), ledger)
    assert held(db, token) and left(db, token) > 600


def test_claim_is_renewed_while_its_own_run_executes(env) -> None:
    db, repo = env
    worker, other = support.uncapped_workers(2)
    token = short_claim(db, repo, worker)
    started_by_r1(db, repo, worker, token)
    bystander = leases.acquire_lease(repo, other, [TARGET + 1], ttl=120)
    live = run(R1, worker, "in_progress", iso(-60))
    owners.reconcile(repo, getter(), Live(live), ledger)
    assert left(db, token) > 600
    assert left(db, bystander) <= 120


def test_failed_lookup_leaves_the_claim_to_expire(env) -> None:
    db, repo = env
    worker = support.uncapped_workers(1)[0]
    token = short_claim(db, repo, worker)
    started_by_r1(db, repo, worker, token)

    def broken(_path: str) -> object:
        raise RuntimeError("gh api failed")

    result = owners.reconcile(repo, broken, Live(), ledger)
    assert [o.state for o in result] == ["unknown"]
    assert held(db, token) and left(db, token) <= 120


def load_controller():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / ".github/scripts/factory-work-controller.py"
    spec = importlib.util.spec_from_file_location("factory_work_controller_owners", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("r2_live", [False, True])
def test_controller_renews_the_live_owner_and_releases_the_finished_one(
    env, monkeypatch: pytest.MonkeyPatch, r2_live: bool
) -> None:
    db, repo = env
    finished, live = support.uncapped_workers(2)
    stale = short_claim(db, repo, finished)
    started_by_r1(db, repo, finished, stale)
    current = leases.acquire_lease(repo, live, [TARGET + 1], ttl=120)
    entry_runs = [run("45500000001", live, "in_progress", iso(5))]
    if r2_live:
        entry_runs.append(run(R2, finished, "in_progress", iso(3600)))
    completed = run(R1, finished, "completed", iso(-60))

    def gh_json(args: list[str], **_kwargs: object) -> object:
        path = args[-1]
        if path.endswith(f"/actions/runs/{R1}"):
            return completed
        live_entry = "free-model-factory-entry.yml/runs?status=in_progress" in path
        return [{"workflow_runs": entry_runs if live_entry else []}]

    controller = load_controller()
    monkeypatch.setattr(controller, "REPO", repo)
    monkeypatch.setattr(controller, "gh_json", gh_json)
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: [])
    assert controller.reconcile_stale_leases() == []
    assert not held(db, stale)
    assert left(db, current) > 600
