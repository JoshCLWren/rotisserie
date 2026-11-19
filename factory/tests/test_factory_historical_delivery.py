"""Historical checkpoint PR receipts require trusted immutable provenance."""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github/scripts"))
delivery = importlib.import_module("factory_historical_delivery")

REPO = "owner/repo"
RUN = "12345"
WORKER = "46"
HEAD = "a" * 40
SOURCE = "b" * 40
BASE = "c" * 40
MERGE = "d" * 40


def pr_data(*, target: str = "main", merged: bool = True,
            body: str | None = None) -> dict:
    """Create a source-run-linked canonical work PR."""
    return {
        "number": 67, "body": body or f"<!-- factory-checkpoint-handoff:v1 run={RUN} -->",
        "base": {"ref": target}, "merged": merged,
        "merge_commit_sha": MERGE,
        "head": {"sha": HEAD, "ref": f"factory/work-checkpoints/issue-42/free/{RUN}",
                 "repo": {"full_name": REPO}},
    }


def test_historical_merged_source_run_proves_immutable_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed execution with bot-saved code earns separate PR/merge receipts."""
    pr = pr_data()
    comments = [
        {"user": {"login": "github-actions[bot]"},
         "body": f"<!-- factory-persisted-handoff:v1 run={RUN} head={SOURCE} -->"},
    ]
    monkeypatch.setattr(delivery, "obj", lambda endpoint: (
        pr if "/pulls/" in endpoint else {"status": "ahead"}))
    monkeypatch.setattr(delivery, "verified_run", lambda _repo, _run: {
        "status": "completed"})
    monkeypatch.setattr(delivery, "source_worker", lambda _run: WORKER)
    monkeypatch.setattr(delivery, "pages", lambda _endpoint: comments)
    monkeypatch.setattr(delivery, "snapshot_for_run", lambda *_args: {
        "base": BASE, "tree": "e" * 40, "head": SOURCE})
    monkeypatch.setattr(delivery, "incorporated", lambda *_args: True)
    result = delivery.receipt_for_pr(REPO, 67)
    assert result == {"run_id": RUN, "worker": WORKER, "saved": True,
                      "checkpoint_sha": SOURCE, "published": True,
                      "work_pr": 67, "validated": False,
                      "merged": True, "merge_sha": MERGE,
                      "merge_basis": "pr_ancestry", "exact_bytes_included": True}


def test_unverified_source_never_credits_code_or_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR existence, foreign comments and wrong bases cannot create receipts."""
    pr = pr_data()
    monkeypatch.setattr(delivery, "obj", lambda endpoint: pr)
    monkeypatch.setattr(delivery, "verified_run", lambda *_args: {
        "status": "completed"})
    monkeypatch.setattr(delivery, "source_worker", lambda *_args: WORKER)
    monkeypatch.setattr(delivery, "pages", lambda *_args: [
        {"user": {"login": "untrusted"},
         "body": f"<!-- factory-persisted-handoff:v1 run={RUN} head={SOURCE} -->"},
    ])
    assert delivery.receipt_for_pr(REPO, 67) is None
    pr["base"]["ref"] = "release"
    assert delivery.receipt_for_pr(REPO, 67) is None
    pr["base"]["ref"] = "main"
    pr["body"] = "No verified source-run marker"
    assert delivery.receipt_for_pr(REPO, 67) is None


def test_backfill_retains_saved_credit_when_merge_bytes_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later repair preserves PR history but not original-byte proof."""
    pr = pr_data()
    monkeypatch.setattr(delivery, "obj", lambda endpoint: (
        pr if "/pulls/" in endpoint else {"status": "ahead"}))
    monkeypatch.setattr(delivery, "verified_run", lambda *_args: {
        "status": "completed"})
    monkeypatch.setattr(delivery, "source_worker", lambda *_args: WORKER)
    monkeypatch.setattr(delivery, "pages", lambda *_args: [{
        "user": {"login": "github-actions[bot]"},
        "body": f"<!-- factory-persisted-handoff:v1 run={RUN} head={SOURCE} -->",
    }])
    monkeypatch.setattr(delivery, "snapshot_for_run", lambda *_args: {
        "head": SOURCE, "base": BASE, "tree": "e" * 40})
    monkeypatch.setattr(delivery, "incorporated", lambda _repo, _source, destination:
                        destination == SOURCE)
    result = delivery.receipt_for_pr(REPO, 67)
    assert result and result["saved"] and result["published"]
    assert result["merged"] is True
    assert result["merge_basis"] == "pr_ancestry"
    assert result["exact_bytes_included"] is False
    writes: list[dict] = []
    summary = delivery.backfill(
        REPO, [67], receipt_writer=lambda _repo, rows: writes.extend(rows))
    assert summary["saved"] == [67] and summary["merged"] == [67]
    assert len(writes) == 1 and writes[0]["exact_bytes_included"] is False


def test_source_commit_metadata_matches_exact_worker_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot comment alone cannot spoof another worker's checkpoint."""
    message = (
        f"checkpoint(factory): worker {WORKER} run {RUN} issue #42 "
        f"(persisted work; validation pending)\n\nBase-commit: {BASE}\n"
    )
    commit = {"sha": SOURCE, "message": message,
              "tree": {"sha": "e" * 40}, "parents": []}
    monkeypatch.setattr(delivery, "obj", lambda _: commit)
    assert delivery.snapshot_for_run(REPO, SOURCE, RUN, WORKER) == {
        "head": SOURCE, "tree": "e" * 40, "base": BASE,
    }
    with pytest.raises(ValueError, match="does not prove"):
        delivery.snapshot_for_run(REPO, SOURCE, RUN, "99")


def test_recent_pr_scan_is_bounded_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large PR history cannot make an unbounded GitHub API sweep."""
    current = datetime(2026, 11, 18, 13, tzinfo=UTC)
    rows = [pr_data() for _ in range(100)]
    for i, item in enumerate(rows):
        item["number"] = i + 1
        item["merged_at"] = "2026-11-18T12:00:00Z"
    second = [{**pr_data(), "number": 101,
               "merged_at": "2026-11-18T12:00:00Z"}]
    calls: list[str] = []

    def fake_api(path: str) -> list[dict]:
        calls.append(path)
        return rows if "&page=1" in path else second

    monkeypatch.setattr(delivery, "api", fake_api)
    results = delivery.recent_candidate_prs(
        REPO, now=current, days=3, max_pages=3, max_prs=110)
    assert len(results) == 101 and results[-1] == 101
    assert len(calls) == 2


def test_recording_is_idempotent_at_trusted_ledger_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated reconciliation never reclassifies a failed worker as success."""
    from factory_state_ledger import checkpoint_evidence_events

    rows = [{"worker": WORKER, "run_id": RUN, "saved": True,
             "checkpoint_sha": SOURCE, "published": True, "work_pr": 67,
             "merged": True, "merge_sha": MERGE}]
    first = checkpoint_evidence_events(REPO, rows[0])
    second = checkpoint_evidence_events(REPO, rows[0])
    assert len(first) == 3 and [item["event_id"] for item in first] == [
        item["event_id"] for item in second
    ]
    assert not any(item["event_type"] == "worker_completed" for item in first)


def test_incident_backfill_extracts_pr_candidates_but_not_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only referenced PR IDs are discovered; another verifier grants receipts."""
    body = (
        "Source runs recorded unsuccessful terminal outcomes even though PRs later merged:\n"
        "| Actions source run | Merged PR | Terminal |\n"
        "| --- | --- | --- |\n"
        "| [44000000001](https://github.com/owner/repo/actions/runs/44000000001)"
        " | #5123 | work_failure |\n"
        "| [44000000002](https://github.com/owner/repo/actions/runs/44000000002)"
        " | #5124 | agent_timeout |\n"
    )
    monkeypatch.setattr(delivery, "obj", lambda *_args: {"body": body})
    assert delivery.incident_prs(REPO, 23) == [5123, 5124]
    assert delivery.incident_prs(REPO, 23, max_prs=1) == [5123]
    assert delivery.incident_prs(REPO, 23) != [23]


def test_merged_pr_without_checkpoint_ancestry_gets_no_source_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot comment cannot credit a source run dropped before the PR merged."""
    pr = pr_data()
    monkeypatch.setattr(delivery, "obj", lambda endpoint: (
        pr if "/pulls/" in endpoint else {"status": "diverged"}))
    monkeypatch.setattr(delivery, "verified_run", lambda *_args: {
        "status": "completed"})
    monkeypatch.setattr(delivery, "source_worker", lambda *_args: WORKER)
    monkeypatch.setattr(delivery, "pages", lambda *_args: [{
        "user": {"login": "github-actions[bot]"},
        "body": f"<!-- factory-persisted-handoff:v1 run={RUN} head={SOURCE} -->",
    }])
    monkeypatch.setattr(delivery, "snapshot_for_run", lambda *_args: {
        "head": SOURCE, "base": BASE, "tree": "e" * 40})
    assert delivery.receipt_for_pr(REPO, 67) is None


def test_multiple_source_checkpoint_snapshots_use_latest_verified_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run may save several immutable heads while one worker is coding."""
    old, latest = "a" * 40, "b" * 40
    pr = pr_data()
    monkeypatch.setattr(delivery, "obj", lambda endpoint: (
        pr if "/pulls/" in endpoint else {"status": "ahead"}))
    monkeypatch.setattr(delivery, "verified_run", lambda *_args: {
        "status": "completed"})
    monkeypatch.setattr(delivery, "source_worker", lambda *_args: WORKER)
    monkeypatch.setattr(delivery, "pages", lambda *_args: [
        {"user": {"login": "github-actions[bot]"},
         "body": f"<!-- factory-persisted-handoff:v1 run={RUN} head={sha} -->"}
        for sha in (old, latest)
    ])
    monkeypatch.setattr(delivery, "snapshot_for_run",
                        lambda _repo, sha, _run, _worker: {
                            "head": sha, "base": BASE, "tree": "e" * 40})
    monkeypatch.setattr(delivery, "incorporated", lambda *_args: True)
    result = delivery.receipt_for_pr(REPO, 67)
    assert result and result["checkpoint_sha"] == latest
