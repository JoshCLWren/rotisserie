"""Tests for deterministic release reconciliation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import release_reconcile as reconcile


def _source(number: int = 12) -> reconcile.Source:
    return reconcile.Source(
        number=number,
        merged_at="2026-09-25T12:00:00Z",
        merge_commit_sha=f"{number:040d}",
        title=f"PR {number}",
    )


def _attempt(code: int, output: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["opencode"],
        returncode=code,
        stdout=output,
        stderr="",
    )


def test_existing_source_is_idempotent(monkeypatch):
    """Existing durable sources must not invoke a model."""
    monkeypatch.setattr(
        reconcile,
        "_check_source",
        lambda repository, source: reconcile.CheckResult(True, {"visibility": "public"}),
    )

    def fail_model(*args, **kwargs):
        raise AssertionError("model should not run")

    monkeypatch.setattr(reconcile, "_run_model", fail_model)

    result = reconcile._process_source("JoshCLWren/comic-pile", _source(), ["model-a"])

    assert result["status"] == "already_present"


@pytest.mark.parametrize(
    ("visibility", "expected"),
    [
        ("public", "newly_published"),
        ("internal", "newly_skipped_internal"),
    ],
)
def test_durable_postcondition_decides_success(monkeypatch, visibility, expected):
    """Publish and skip both pass only after the exact source exists."""
    checks = iter(
        [
            reconcile.CheckResult(False, None),
            reconcile.CheckResult(True, {"visibility": visibility}),
        ]
    )
    monkeypatch.setattr(
        reconcile,
        "_check_source",
        lambda repository, source: next(checks),
    )
    monkeypatch.setattr(
        reconcile,
        "_run_model",
        lambda repository, source, model: _attempt(0),
    )

    result = reconcile._process_source("JoshCLWren/comic-pile", _source(), ["model-a"])

    assert result["status"] == expected


def test_false_green_falls_through_to_next_model(monkeypatch):
    """Exit zero without a source record is not success."""
    checks = iter(
        [
            reconcile.CheckResult(False, None),
            reconcile.CheckResult(False, None),
            reconcile.CheckResult(True, {"visibility": "public"}),
        ]
    )
    models: list[str] = []
    monkeypatch.setattr(
        reconcile,
        "_check_source",
        lambda repository, source: next(checks),
    )

    def run_model(repository, source, model):
        models.append(model)
        return _attempt(0)

    monkeypatch.setattr(reconcile, "_run_model", run_model)

    result = reconcile._process_source(
        "JoshCLWren/comic-pile",
        _source(),
        ["model-a", "model-b"],
    )

    assert result["status"] == "newly_published"
    assert models == ["model-a", "model-b"]


def test_rate_limit_falls_through_to_next_model(monkeypatch):
    """A 429 may use a healthy fallback model."""
    checks = iter(
        [
            reconcile.CheckResult(False, None),
            reconcile.CheckResult(False, None),
            reconcile.CheckResult(True, {"visibility": "internal"}),
        ]
    )
    attempts = iter([_attempt(1, "HTTP 429"), _attempt(0)])
    monkeypatch.setattr(
        reconcile,
        "_check_source",
        lambda repository, source: next(checks),
    )
    monkeypatch.setattr(
        reconcile,
        "_run_model",
        lambda repository, source, model: next(attempts),
    )

    result = reconcile._process_source(
        "JoshCLWren/comic-pile",
        _source(),
        ["model-a", "model-b"],
    )

    assert result["status"] == "newly_skipped_internal"


def test_non_rate_limit_failure_is_not_hidden(monkeypatch):
    """Unexpected model failures fail that source instead of masking the error."""
    checks = iter(
        [
            reconcile.CheckResult(False, None),
            reconcile.CheckResult(False, None),
        ]
    )
    seen: list[str] = []
    monkeypatch.setattr(
        reconcile,
        "_check_source",
        lambda repository, source: next(checks),
    )

    def run_model(repository, source, model):
        seen.append(model)
        return _attempt(7, "broken")

    monkeypatch.setattr(reconcile, "_run_model", run_model)

    result = reconcile._process_source(
        "JoshCLWren/comic-pile",
        _source(),
        ["model-a", "model-b"],
    )

    assert result["status"] == "failed"
    assert seen == ["model-a"]


def test_all_false_green_models_fail(monkeypatch):
    """Every model exiting zero without durability must fail the job source."""
    monkeypatch.setattr(
        reconcile,
        "_check_source",
        lambda repository, source: reconcile.CheckResult(False, None),
    )
    monkeypatch.setattr(
        reconcile,
        "_run_model",
        lambda repository, source, model: _attempt(0),
    )

    result = reconcile._process_source(
        "JoshCLWren/comic-pile",
        _source(),
        ["model-a", "model-b"],
    )

    assert result["status"] == "failed"
    assert "false-green models" in str(result["reason"])


def test_multiple_prs_are_processed_independently(monkeypatch):
    """One failed PR must not discard later reconciliation candidates."""

    def process(repository, source, models):
        if source.number == 1:
            return {"pr": 1, "status": "failed"}
        return {"pr": source.number, "status": "already_present"}

    monkeypatch.setattr(reconcile, "_process_source", process)

    results, conflict, unexamined = reconcile.reconcile(
        "JoshCLWren/comic-pile",
        [_source(1), _source(2), _source(3)],
        ["model-a"],
    )

    assert [item["pr"] for item in results] == [1, 2, 3]
    assert conflict is False
    assert unexamined == 0


def test_conflict_stops_reconciliation(monkeypatch):
    """Provenance conflicts stop before later sources are touched."""
    seen: list[int] = []

    def process(repository, source, models):
        seen.append(source.number)
        if source.number == 2:
            raise reconcile.SourceConflictError("HTTP 409 conflict")
        return {"pr": source.number, "status": "already_present"}

    monkeypatch.setattr(reconcile, "_process_source", process)

    results, conflict, unexamined = reconcile.reconcile(
        "JoshCLWren/comic-pile",
        [_source(1), _source(2), _source(3)],
        ["model-a"],
    )

    assert seen == [1, 2]
    assert results[-1]["status"] == "conflict"
    assert conflict is True
    assert unexamined == 1


def test_summary_reports_required_backfill_counters():
    """Machine-readable summaries expose every required reconciliation counter."""
    summary = reconcile._summary(
        "JoshCLWren/comic-pile",
        [
            {"pr": 1, "status": "already_present"},
            {"pr": 2, "status": "newly_published"},
            {"pr": 3, "status": "newly_skipped_internal"},
            {"pr": 4, "status": "failed"},
        ],
        conflict=False,
        unexamined=0,
    )

    assert summary["total_merged_prs_examined"] == 4
    assert summary["already_present"] == 1
    assert summary["newly_published"] == 1
    assert summary["newly_skipped_internal"] == 1
    assert summary["conflicts"] == 0
    assert summary["failures"] == 1
    assert summary["still_missing"] == 1


def test_release_workflow_reactivates_only_for_exact_merged_source_via_neon():
    """No public API, no unbounded scan, no DB secrets in the model job."""
    workflow = Path(".github/workflows/release-writer.yml").read_text()
    assert "scripts/release_reconcile.py" in workflow
    assert '--pr "$INPUT_PR"' in workflow
    assert "--since" not in workflow
    assert "cron:" not in workflow
    assert "pull_request_target:" in workflow
    assert "types: [closed]" in workflow
    assert "branches: [main]" in workflow
    assert "github.event.pull_request.merged == true" in workflow
    assert "github.event.pull_request.number || inputs.pr_number" in workflow
    assert "RELEASE_API_URL" not in workflow
    assert "RELEASE_WRITER_TOKEN" not in workflow
    assert "scripts.release_neon_publish" in workflow
    assert "if: ${{ false }}" not in workflow
    assert "ref: main" in workflow
    model_job = workflow.split("\n  generate:", 1)[1].split("\n  persist:", 1)[0]
    publisher_job = workflow.split("\n  persist:", 1)[1]
    assert "NEON_API_KEY" not in model_job
    assert "RELEASE_NEON_DATABASE_URL" not in model_job
    assert "RELEASE_WRITER_STAGE_FILE" in model_job
    assert "NEON_API_KEY" in publisher_job
    assert "RELEASE_NEON_DATABASE_URL" in publisher_job


def test_factory_workflow_completion_dispatches_release_reconciliation():
    """Factory completion must fan out without pull_request.closed."""
    workflow = Path(".github/workflows/factory-release-reconcile.yml").read_text()

    assert "workflow_run:" in workflow
    assert "Factory Ready Merge Drain" in workflow
    assert "Fixed Model Factory Dispatcher" in workflow
    assert "gh workflow run release-writer.yml" in workflow
    assert "backfill_since" not in workflow
    assert "if: ${{ false }}" in workflow
    assert ' -f "pr_number=$INPUT_PR"' in workflow
