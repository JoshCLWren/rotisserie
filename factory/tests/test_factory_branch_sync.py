"""Exclusive, compare-and-swap branch sync (no network)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / ".github/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("factory_branch_sync_test", SCRIPTS / "factory_branch_sync.py")
assert SPEC is not None and SPEC.loader is not None
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)

HEAD = "a" * 40
MOVED = "b" * 40


class FakeHub:
    def __init__(self, *, contains=False, update_error=None):
        self.calls: list[tuple] = []
        self._contains = contains
        self._update_error = update_error

    def pull(self, number):
        self.calls.append(("pull", number))
        return sync.PullView(number, "open", HEAD, frozenset(), "main")

    def contains(self, head, target):
        self.calls.append(("compare", head, target))
        return self._contains

    def update_branch(self, number, expected_head):
        self.calls.append(("update", number, expected_head))
        if self._update_error is not None:
            raise self._update_error


class FakeLeases:
    consulted = True

    def __init__(self, *, holder=None, token="tok"):
        self._holder = holder
        self._token = token
        self.acquired: list[int] = []
        self.released: list[tuple[int, str]] = []

    def holder(self, number):
        return self._holder

    def acquire(self, number):
        self.acquired.append(number)
        return self._token

    def release(self, number, token):
        self.released.append((number, token))


def snap(**overrides):
    view = dict(number=7, state="open", head=HEAD, labels=frozenset(), base="main")
    view.update(overrides)
    return {7: sync.PullView(**view)}


def test_free_pr_is_synced_under_a_lease_with_compare_and_swap():
    hub, leases = FakeHub(), FakeLeases()
    result = sync.sync_pull(hub, leases, sync.Request(7, HEAD), snapshot=snap())
    assert result.status == sync.SYNCED and result.ok
    # Snapshot given: no PR re-read; one compare, one update on the seen head.
    assert hub.calls == [("compare", HEAD, "main"), ("update", 7, HEAD)]
    assert leases.acquired == [7] and leases.released == [(7, "tok")]


def test_worker_held_pr_is_never_written():
    hub, leases = FakeHub(), FakeLeases(holder="worker 43")
    result = sync.sync_pull(hub, leases, sync.Request(7), snapshot=snap())
    assert result.status == sync.HELD and "worker 43 holds it" in result.text
    assert not leases.acquired and not any(call[0] == "update" for call in hub.calls)


def test_losing_the_lease_race_leaves_the_pr_alone():
    hub, leases = FakeHub(), FakeLeases(token=None)
    result = sync.sync_pull(hub, leases, sync.Request(7), snapshot=snap())
    assert result.status == sync.HELD and not any(call[0] == "update" for call in hub.calls)
    assert not leases.released


def test_already_current_head_needs_nothing():
    hub, leases = FakeHub(contains=True), FakeLeases()
    result = sync.sync_pull(hub, leases, sync.Request(7, target="c" * 40), snapshot=snap())
    assert result.status == sync.CURRENT and hub.calls == [("compare", HEAD, "c" * 40)]
    assert not leases.acquired


def test_moved_head_since_the_plan_is_not_synced():
    hub, leases = FakeHub(), FakeLeases()
    result = sync.sync_pull(hub, leases, sync.Request(7, MOVED), snapshot=snap())
    assert result.status == sync.HEAD_MOVED and hub.calls == []


def test_push_during_sync_is_reported_as_head_moved_and_lease_released():
    error = sync.HubError("Expected head sha didn't match current head ref.", "head-moved")
    hub, leases = FakeHub(update_error=error), FakeLeases()
    result = sync.sync_pull(hub, leases, sync.Request(7), snapshot=snap())
    assert result.status == sync.HEAD_MOVED and leases.released == [(7, "tok")]


def test_conflicting_pr_goes_to_merge_conflict_work_without_any_write():
    hub, leases = FakeHub(), FakeLeases()
    result = sync.sync_pull(hub, leases, sync.Request(7), snapshot=snap(merge_state="DIRTY"))
    assert result.status == sync.CONFLICTS and "merge-conflict work" in result.text
    assert hub.calls == [] and not leases.acquired


def test_update_branch_conflict_is_reported_not_retried():
    error = sync.HubError("merge conflict between base and head", "conflict")
    hub, leases = FakeHub(update_error=error), FakeLeases()
    result = sync.sync_pull(hub, leases, sync.Request(7), snapshot=snap())
    assert result.status == sync.CONFLICTS
    assert [call for call in hub.calls if call[0] == "update"] == [("update", 7, HEAD)]


def test_pr_missing_from_the_open_snapshot_is_closed():
    hub, leases = FakeHub(), FakeLeases()
    result = sync.sync_pull(hub, leases, sync.Request(8), snapshot=snap())
    assert result.status == sync.CLOSED and hub.calls == []


def test_label_fallback_says_neon_was_not_consulted_and_respects_owner_labels():
    pulls = snap(labels=frozenset({"factory", "factory:43"}))
    hub = FakeHub()
    result = sync.sync_pull(hub, sync.LabelLeases(hub, pulls), sync.Request(7), snapshot=pulls)
    assert result.status == sync.HELD and "owner label factory:43" in result.text
    assert "Neon leases were not consulted" in result.text and result.neon is False
    assert not any(call[0] == "update" for call in hub.calls)


def test_run_caps_writes_not_reads():
    pulls = {
        n: sync.PullView(n, "open", HEAD, frozenset(), "main") for n in (1, 2, 3)
    }
    hub, leases = FakeHub(), FakeLeases()
    outcomes = sync.run(hub, leases, [sync.Request(n) for n in (1, 2, 3)], limit=2, snapshot=pulls)
    assert [outcome.pr for outcome in outcomes] == [1, 2]


def test_snapshot_reads_rest_and_gh_pr_list_rows():
    rest = {"number": 5, "state": "open", "head": {"sha": HEAD}, "base": {"ref": "main"},
            "labels": [{"name": "factory-sync-main"}], "mergeable_state": "behind"}
    listed = {"number": 6, "headRefOid": MOVED, "baseRefName": "trunk",
              "labels": [{"name": "factory:12"}], "mergeStateStatus": "DIRTY"}
    flat = {"number": 7, "head": HEAD, "base": "main", "labels": ["x"]}
    pulls = sync.snapshot_from_rows([rest, listed, flat])
    assert pulls[5].labels == {"factory-sync-main"} and pulls[5].base == "main"
    assert pulls[6].head == MOVED and pulls[6].conflicted and pulls[6].state == "open"
    assert pulls[7].labels == {"x"}
    assert [r.pr for r in sync.queued_requests(pulls)] == [5]


def test_update_errors_are_classified():
    assert sync.classify_update_error("gh: merge conflict between base and head") == "conflict"
    assert sync.classify_update_error("expected head sha didn't match current head ref") == "head-moved"
    assert sync.classify_update_error("HTTP 502") == "other"


def test_queued_tick_costs_one_list_plus_one_compare_and_update_per_pr(monkeypatch, capsys):
    calls: list[str] = []
    rows = [
        {"number": 7, "state": "open", "head": HEAD, "base": "main",
         "labels": ["factory-sync-main"]},
        {"number": 8, "state": "open", "head": MOVED, "base": "main", "labels": []},
    ]

    class Hub(sync.GhHub):
        def open_pulls(self):
            calls.append("list")
            return sync.snapshot_from_rows(rows)

        def _api(self, path, *args):
            calls.append(path.split("?")[0])
            return {"status": "behind"} if path.startswith("compare/") else {}

    monkeypatch.setattr(sync, "GhHub", Hub)
    monkeypatch.delenv("FACTORY_STATE_DSN_FILE", raising=False)
    monkeypatch.delenv("FACTORY_NEON_LEASE_REQUIRED", raising=False)
    assert sync.main(["--repo", "o/r", "--queued"]) == 0
    assert calls == [
        "list",
        f"compare/main...{HEAD}",
        "pulls/7/update-branch",
        "issues/7/labels/factory-sync-main",
    ]
    [outcome] = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "synced" and outcome["neon"] is False


def test_trusted_runtime_refuses_label_fallback_when_neon_is_required(monkeypatch):
    class Hub(sync.GhHub):
        def open_pulls(self):
            return sync.snapshot_from_rows(
                [{"number": 7, "head": HEAD, "base": "main", "labels": ["factory-sync-main"]}]
            )

    monkeypatch.setattr(sync, "GhHub", Hub)
    monkeypatch.delenv("FACTORY_STATE_DSN_FILE", raising=False)
    monkeypatch.setenv("FACTORY_NEON_LEASE_REQUIRED", "true")
    assert sync.main(["--repo", "o/r", "--queued"]) == 1


def test_neon_leases_name_the_holder_and_never_accept_an_unconfigured_ledger():
    class Ledger:
        def target_lease(self, repo, number):
            return {"worker": "43"} if number == 1 else None

        def acquire_operation_lease(self, repo, holder, number):
            return ""

        def release_operation_lease(self, repo, number, token, reason):
            return True

    leases = sync.NeonLeases("o/r", "scheduler-1", Ledger())
    assert leases.holder(1) == "worker 43" and leases.holder(2) is None
    with pytest.raises(RuntimeError):
        leases.acquire(2)


def test_dispatcher_runs_the_bounded_leased_sync_pass():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/fixed-model-factory-dispatch.yml")
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    steps = {step.get("name"): step for step in jobs["dispatch"]["steps"]}
    step = steps["Sync queued factory PRs with main under Neon leases"]
    assert "steps.api_budget.outputs.allow == 'true'" in step["if"]
    assert "!(inputs.target || inputs.provider)" in step["if"]
    assert "steps.main_health.outputs.state != 'red'" in step["if"]
    assert "factory_branch_sync.py --queued --max 5" in step["run"]
    assert "--force" not in step["run"] and "git push" not in step["run"]
    names = list(steps)
    assert names.index("Configure shared Neon factory discovery cache") < names.index(
        "Sync queued factory PRs with main under Neon leases"
    )
