"""Regressions for bounded recovery and factory incident routing."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


@pytest.fixture
def recovery(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the trusted deployment recovery helper."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "factory_deploy_recovery", SCRIPTS / "factory_deploy_recovery.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def failed_run() -> dict[str, object]:
    """Return an actual failed deployment with a failed health smoke."""
    return {
        "id": 11,
        "name": "Deploy Production",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "failure",
        "updated_at": "2026-11-08T10:00:00Z",
        "html_url": "https://github.com/example/repo/actions/runs/11",
        "jobs": [
            {
                "name": "Migrate Neon and deploy exact main SHA",
                "conclusion": "failure",
                "steps": [{"name": "Smoke production Rust health", "conclusion": "failure"}],
            }
        ],
    }


def repair() -> dict[str, object]:
    """Return a newly merged deploy repair on main."""
    return {
        "number": 42,
        "baseRefName": "main",
        "mergeCommit": {"oid": "b" * 40},
        "mergedAt": "2026-11-08T11:00:00Z",
        "labels": [{"name": "deploy-fix"}],
    }


def test_repair_is_consumed_by_next_failed_attempt(recovery: ModuleType) -> None:
    """A prior merged fix cannot authorize endless retries after another failure."""
    run = failed_run()
    assert recovery.repair_candidates(run, [repair()]) == [(42, "b" * 40)]
    run["updated_at"] = "2026-11-08T12:00:00Z"
    assert recovery.repair_candidates(run, [repair()]) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseRefName", "other"),
        ("mergeCommit", None),
        ("mergedAt", None),
        ("labels", []),
        ("mergeCommit", {"oid": "invalid"}),
    ],
)
def test_unmerged_unlabelled_and_wrong_base_do_not_authorize(
    recovery: ModuleType, field: str, value: object
) -> None:
    """Only a complete merged repair record can grant recovery."""
    pull = repair()
    pull[field] = value
    assert recovery.repair_candidates(failed_run(), [pull]) == []


def test_repair_must_be_in_current_main(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged fix on a divergent/reset history cannot grant an attempt."""
    monkeypatch.setattr(recovery, "gh_json", lambda args: [repair()])
    monkeypatch.setattr(
        recovery.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 1)
    )
    assert recovery.merged_repair(failed_run(), "example/repo") is None
    monkeypatch.setattr(
        recovery.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0)
    )
    assert recovery.merged_repair(failed_run(), "example/repo") == 42


def exercise_reconcile(
    recovery: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    run: dict[str, object],
    incidents: list[dict[str, object]],
) -> list[list[str]]:
    """Run reconciliation against captured GitHub operations."""
    calls: list[list[str]] = []

    def api(arguments: list[str]) -> object:
        calls.append(arguments)
        if arguments and arguments[0] == "api" and "actions/workflows/deploy-production.yml/runs" in arguments[-1]:
            assert "--paginate" not in arguments
            assert "per_page=20" in arguments[-1]
            return {"workflow_runs": [run]}
        if arguments[:2] == ["issue", "list"]:
            return incidents
        return {}

    monkeypatch.setattr(recovery, "gh_json", api)
    recovery.reconcile("example/repo")
    return calls


def test_failure_creates_actionable_factory_incident(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual failure produces an executable, deduplicated repair task."""
    calls = exercise_reconcile(recovery, monkeypatch, failed_run(), [])
    create = calls[-1]
    assert "POST" in create
    assert "repos/example/repo/issues" in create
    assert "labels[]=deploy-fix" in create
    assert "labels[]=factory:unowned" in create
    assert "labels[]=ralph-status:pending" in create
    assert any("Smoke production Rust health" in arg for arg in create)
    assert any("actions/runs/11" in arg for arg in create)


def test_failure_refresh_preserves_live_lease(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat changes only incident evidence, leaving labels/ownership intact."""
    incidents = [{"number": 8, "body": recovery.MARKER, "labels": [{"name": "factory:local"}]}]
    calls = exercise_reconcile(recovery, monkeypatch, failed_run(), incidents)
    assert "PATCH" in calls[-1]
    assert "repos/example/repo/issues/8" in calls[-1]
    assert not any("labels" in arg for arg in calls[-1])
    # Identical heartbeats do not spam comments or update issue activity.
    incidents[0]["body"] = recovery.incident_body(failed_run())
    calls = exercise_reconcile(recovery, monkeypatch, failed_run(), incidents)
    assert len(calls) == 2


def test_success_needs_both_real_smokes(recovery: ModuleType) -> None:
    """A successful recovery requires both real production smoke probes."""
    run = failed_run()
    run["conclusion"] = "success"
    run["jobs"] = [{"name": recovery.DEPLOY_JOB_NAME, "conclusion": "skipped"}]
    assert not recovery.successful_smokes(run)
    run["jobs"] = [{"name": recovery.DEPLOY_JOB_NAME, "conclusion": "success", "steps": []}]
    assert not recovery.successful_smokes(run)
    run["jobs"] = [
        {
            "name": recovery.DEPLOY_JOB_NAME,
            "conclusion": "success",
            "steps": [{"name": name, "conclusion": "success"} for name in recovery.SMOKES],
        }
    ]
    assert recovery.successful_smokes(run)


def test_green_attempt_closes_only_managed_incidents(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proven recovery closes its own incident, with a verification comment."""
    run = failed_run()
    run["conclusion"] = "success"
    run["jobs"] = [
        {
            "name": recovery.DEPLOY_JOB_NAME,
            "conclusion": "success",
            "steps": [{"name": name, "conclusion": "success"} for name in recovery.SMOKES],
        }
    ]
    calls = exercise_reconcile(
        recovery,
        monkeypatch,
        run,
        [
            {"number": 8, "body": recovery.MARKER},
            {"number": 9, "body": "Unrelated deploy-fix"},
        ],
    )
    assert "repos/example/repo/issues/8/comments" in calls[-2]
    assert "state=closed" in calls[-1]
    assert not any("issues/9" in arg for call in calls for arg in call)


def test_recovered_incident_keeps_active_worker_lease(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful external deploy does not steal an active repair issue."""
    run = failed_run()
    run["conclusion"] = "success"
    run["jobs"] = [
        {
            "name": recovery.DEPLOY_JOB_NAME,
            "conclusion": "success",
            "steps": [{"name": name, "conclusion": "success"} for name in recovery.SMOKES],
        }
    ]
    calls = exercise_reconcile(
        recovery,
        monkeypatch,
        run,
        [
            {
                "number": 8,
                "body": recovery.MARKER,
                "labels": [{"name": "factory:local"}],
            }
        ],
    )
    assert len(calls) == 2


@pytest.mark.parametrize("invalid", [None, "sha", "run_id", "run_attempt"])
def test_exact_attempt_revision_artifact(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch, invalid: str | None
) -> None:
    """Only matching run/attempt metadata supplies the selected main revision."""
    run = failed_run()
    run["run_attempt"] = 2
    payload = {"sha": "b" * 40, "run_id": "11", "run_attempt": "2"}
    if invalid:
        payload[invalid] = "invalid"

    def download(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "deployment-attempt-2" in arguments
        Path(arguments[-1], "deployment-attempt.json").write_text(json.dumps(payload))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(recovery.subprocess, "run", download)
    selected = recovery.attempt_revision(run, "example/repo")
    assert selected == (None if invalid else "b" * 40)
    body = recovery.incident_body(run, selected)
    assert f"Workflow trigger SHA: `{'a' * 40}`" in body
    assert ("unavailable" in body) is bool(invalid)
    if not invalid:
        assert f"Selected deployment attempt SHA: `{'b' * 40}`" in body


def test_missing_historical_attempt_artifact_is_truthful(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired/missing/API-unavailable evidence is never replaced by trigger SHA."""
    run = failed_run()
    run["run_attempt"] = 1

    def missing(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["gh", "run", "download"])

    monkeypatch.setattr(recovery.subprocess, "run", missing)
    assert recovery.attempt_revision(run, "example/repo") is None
    assert "unavailable" in recovery.incident_body(run)


def test_missing_deploy_job_evidence_does_not_crash_or_claim_success(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rate-limited job lookup cannot fake a deploy or close its incident."""
    run = failed_run()
    run["conclusion"] = "success"
    run.pop("jobs")
    assert not recovery.successful_smokes(run)
    calls = exercise_reconcile(
        recovery, monkeypatch, run, [{"number": 8, "body": recovery.MARKER}]
    )
    assert not any("PATCH" in call or "state=closed" in call for call in calls)
    assert not any("POST" in call for call in calls)



def test_rate_limited_gh_api_defers_without_masking_other_403(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a true GitHub quota rejection is a retryable no-op."""
    def failing_api(message: str):
        def run(*args: object, **kwargs: object) -> object:
            raise subprocess.CalledProcessError(1, ["gh", "api"], stderr=message)
        return run

    monkeypatch.setattr(
        recovery.subprocess, "run",
        failing_api("HTTP 403: API rate limit exceeded for installation"),
    )
    with pytest.raises(recovery.GitHubQuotaDeferredError):
        recovery.gh_json(["api", "repos/example/repo/actions/runs"])

    monkeypatch.setattr(
        recovery.subprocess, "run",
        failing_api("HTTP 403: Resource not accessible by integration"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        recovery.gh_json(["api", "repos/example/repo/actions/runs"])


def test_quota_deferred_main_does_not_mutate_incidents(
    recovery: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The existing 15-minute watchdog retries, without fake production success."""
    def deferred(_: str) -> None:
        raise recovery.GitHubQuotaDeferredError("GitHub API quota exhausted")
    monkeypatch.setattr(recovery, "reconcile", deferred)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setattr("sys.argv", ["factory_deploy_recovery.py"])
    recovery.main()
    assert "deferred" in capsys.readouterr().out


def test_unverifiable_deploy_job_list_cannot_invent_attempt(
    recovery: ModuleType
) -> None:
    """Rate-limited job history must not create a speculative repair issue."""
    run = failed_run()
    run.pop("jobs")
    assert recovery.latest_attempted_deploy_run(
        {"workflow_runs": [run]}, jobs_loader=lambda _: None,
    ) is None
