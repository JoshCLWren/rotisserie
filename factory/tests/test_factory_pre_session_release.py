"""Exercise trusted lease cleanup when a factory aborts before its worker starts."""

import ast
import json
import os
from pathlib import Path
import subprocess
from typing import cast

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/free-model-factory-run.yml"
SCRIPTS = ROOT / ".github/scripts"


def _generated_slot_ids(count: int = 3) -> list[str]:
    """Real provider-derived worker identities (the TSV roster was removed)."""
    import sys

    sys.path.insert(0, str(SCRIPTS))
    import factory_worker_slots

    return sorted(slot.worker for slot in factory_worker_slots.slots())[:count]


def _stage_release_scripts(tmp_path: Path) -> Path:
    """Copy the real slot resolver and a controller stand-in into a sandbox."""
    scripts = tmp_path / ".github/scripts"
    scripts.mkdir(parents=True)
    for name in ("factory_worker_slots.py", "factory_provider_registry.py"):
        (scripts / name).write_text((SCRIPTS / name).read_text())
    controller = scripts / "factory-work-controller.py"
    controller.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    return controller


def test_cleanup_waits_for_factory_and_excludes_started_sessions() -> None:
    """A failed smoke releases promptly; an executing worker owns its lifecycle."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    factory = workflow["jobs"]["factory"]
    cleanup = workflow["jobs"]["release_pre_session_lease"]
    assert cleanup["needs"] == "factory"
    assert cleanup["if"] == (
        "${{ always() && needs.factory.outputs.worker_session_started != 'true' }}"
    )
    assert factory["outputs"]["worker_session_started"] == (
        "${{ steps.worker_session.outputs.started }}"
    )
    session = next(step for step in factory["steps"] if step.get("id") == "worker_session")
    assert session["run"].index("started=true") < session["run"].index(
        "bash .github/scripts/free-model-factory-worker.sh"
    )
    # Serialization must cover both jobs so a successor cannot acquire a new
    # lease for this worker while its preceding failed startup is recovering.
    assert workflow["concurrency"]["group"] == "fixed-model-factory-${{ inputs.worker }}"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_post_session_cleanup_runs_under_worker_lock() -> None:
    """After a model session, the trusted cleanup releases both ownership ledgers."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    cleanup = workflow["jobs"]["release_post_session_lease"]
    assert cleanup["needs"] == "factory"
    assert cleanup["if"] == (
        "${{ always() && needs.factory.outputs.worker_session_started == 'true' }}"
    )
    assert workflow["concurrency"]["group"] == "fixed-model-factory-${{ inputs.worker }}"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert cleanup["runs-on"] == "ubuntu-latest"
    assert cleanup["steps"][0]["with"] == {"ref": "main", "persist-credentials": False}
    assert cleanup["steps"][2]["uses"] == "./.github/actions/factory-state-cache"
    assert cleanup["steps"][2]["with"] == {
        "neon-api-key": "${{ secrets.NEON_API_KEY }}",
        "neon-project-id": "${{ vars.NEON_PROJECT_ID }}",
    }
    release = cleanup["steps"][3]
    assert "post-session-exit-run-${GITHUB_RUN_ID}" in release["run"]
    assert "refusing split release" in release["run"]
    assert cleanup["permissions"] == {
        "contents": "read", "issues": "write", "pull-requests": "write",
    }
    assert "factory-state-cache" not in json.dumps(workflow["jobs"]["factory"])


def test_post_session_cleanup_executes_token_bound_release(tmp_path: Path) -> None:
    """Run the real job command with a stand-in controller and restricted DSN."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    release = workflow["jobs"]["release_post_session_lease"]["steps"][3]
    _stage_release_scripts(tmp_path)
    worker = _generated_slot_ids(1)[0]
    dsn = tmp_path / "restricted-dsn"
    dsn.write_text("test-only-connection")
    result = subprocess.run(
        ["bash", "-c", release["run"]], cwd=tmp_path,
        env={**os.environ, "FACTORY_WORKER": worker, "GITHUB_RUN_ID": "43455638659",
             "FACTORY_STATE_DSN_FILE": str(dsn), "FACTORY_ASSIGNMENT": '{"claim_id":"test-claim"}'},
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == [
        "release", "--worker", worker, "--claim-id", "test-claim", "--reason",
        "post-session-exit-run-43455638659",
    ]


def test_cleanup_uses_a_fresh_trusted_runner_and_workflow_token() -> None:
    """Never run recovery code or carry model credentials from the smoke checkout."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    cleanup = workflow["jobs"]["release_pre_session_lease"]
    assert cleanup["runs-on"] == "ubuntu-latest"
    assert "env" not in cleanup
    checkout = cleanup["steps"][0]
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"] == {"ref": "main", "persist-credentials": False}
    assert cleanup["steps"][1]["uses"] == "./.github/actions/factory-syntax"
    neon = cleanup["steps"][2]
    assert neon["uses"] == "./.github/actions/factory-state-cache"
    assert neon["with"] == {
        "neon-api-key": "${{ secrets.NEON_API_KEY }}",
        "neon-project-id": "${{ vars.NEON_PROJECT_ID }}",
    }
    # The privileged DB action belongs to cleanup, never the model job.
    assert "factory-state-cache" not in json.dumps(workflow["jobs"]["factory"])
    assert "NEON_API_KEY" not in json.dumps(workflow["jobs"]["factory"])
    release = cleanup["steps"][3]
    assert "FACTORY_STATE_DSN_FILE" in release["run"]
    assert "refusing split release" in release["run"]
    assert release["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "FACTORY_WORKER": "${{ inputs.worker }}",
        "FACTORY_ASSIGNMENT": "${{ inputs.assignment }}",
    }
    assert cleanup["permissions"] == {
        # PR-target label replacement returns 403 with pull-requests:read,
        # even though the same job can release an ordinary issue lease.
        "contents": "read", "issues": "write", "pull-requests": "write",
    }
    entry = yaml.safe_load((ROOT / ".github/workflows/free-model-factory-entry.yml").read_text())
    assert entry["permissions"]["pull-requests"] == "write"
    assert workflow["permissions"]["pull-requests"] == "write"


@pytest.mark.parametrize("worker", _generated_slot_ids())
def test_cleanup_calls_existing_controller_with_run_provenance(
    tmp_path: Path, worker: str,
) -> None:
    """Execute the workflow shell against a controller stand-in, without GitHub writes."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    release = workflow["jobs"]["release_pre_session_lease"]["steps"][3]
    _stage_release_scripts(tmp_path)
    dsn = tmp_path / "restricted-dsn"
    dsn.write_text("not-a-real-connection")
    result = subprocess.run(
        ["bash", "-c", release["run"]], cwd=tmp_path,
        env={**os.environ, "FACTORY_WORKER": worker, "GITHUB_RUN_ID": "41323597249",
             "FACTORY_STATE_DSN_FILE": str(dsn), "FACTORY_ASSIGNMENT": '{"claim_id":"test-claim"}'},
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == [
        "release", "--worker", worker, "--claim-id", "test-claim", "--reason", "pre-session-abort-run-41323597249",
    ]


@pytest.mark.parametrize("worker", ["", "0", "00", "0104", "local", "104; echo injected"])
def test_cleanup_rejects_invalid_worker_before_invoking_controller(
    tmp_path: Path, worker: str,
) -> None:
    """Malformed dispatch input cannot broaden recovery or execute shell fragments."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    release = workflow["jobs"]["release_pre_session_lease"]["steps"][3]
    result = subprocess.run(
        ["bash", "-c", release["run"]], cwd=tmp_path,
        env={**os.environ, "FACTORY_WORKER": worker, "GITHUB_RUN_ID": "41323597249"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Invalid worker ID; refusing lease recovery\n"

def test_cleanup_rejects_unknown_decimal_worker_before_invoking_controller(
    tmp_path: Path,
) -> None:
    """A well-formed but unprovisioned ID is refused by the real slot resolver."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    release = workflow["jobs"]["release_pre_session_lease"]["steps"][3]
    controller = _stage_release_scripts(tmp_path)
    controller.write_text("raise AssertionError('controller must not run')\n")
    dsn = tmp_path / "restricted-dsn"
    dsn.write_text("test-only-connection")
    result = subprocess.run(
        ["bash", "-c", release["run"]], cwd=tmp_path,
        env={**os.environ, "FACTORY_WORKER": "1000", "GITHUB_RUN_ID": "41323597249",
             "FACTORY_STATE_DSN_FILE": str(dsn), "FACTORY_ASSIGNMENT": '{"claim_id":"c"}'},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "unknown or inactive worker slot" in result.stderr
    assert "controller must not run" not in result.stderr


def test_cleanup_refuses_label_release_without_neon_connection(tmp_path: Path) -> None:
    """Neon outage must not release GitHub labels while retaining the DB claim."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    release = workflow["jobs"]["release_pre_session_lease"]["steps"][3]
    controller = tmp_path / ".github/scripts/factory-work-controller.py"
    controller.parent.mkdir(parents=True)
    controller.write_text("raise AssertionError('controller must not run')\n")
    result = subprocess.run(
        ["bash", "-c", release["run"]], cwd=tmp_path,
        env={**os.environ, "FACTORY_WORKER": "104", "GITHUB_RUN_ID": "41323597249",
             "FACTORY_STATE_DSN_FILE": ""},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Neon factory lease connection missing" in result.stderr


def _load_controller_release(helpers: dict[str, object]):
    """Exercise the real release function without importing its GitHub CLI."""
    source = (ROOT / ".github/scripts/factory-work-controller.py").read_text()
    wanted = {"release_worker", "_retry_transient_github", "TRANSIENT_GITHUB_RE",
              "GITHUB_RETRY_DELAYS"}
    nodes = [
        node for node in ast.parse(source).body
        if (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (isinstance(node, (ast.Assign, ast.AnnAssign)) and any(
            isinstance(t, ast.Name) and t.id in wanted
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        ))
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    sleeps = cast(list[float], helpers.pop("sleeps", []))
    namespace: dict[str, object] = {
        "REPO": "owner/repo",
        "Any": object,
        "Callable": __import__("collections.abc").abc.Callable,
        "re": __import__("re"),
        "time": type("FakeTime", (), {"sleep": staticmethod(sleeps.append)}),
        "sys": __import__("sys"),
        "list_issues": lambda: [],
        "list_prs": lambda: [],
        "owner_of": lambda labels: None,
        "labels_of": lambda item: [],
        "target_owned_by": lambda number, owner: False,
        "target_json": lambda number: {"labels": []},
        "OWNER_RE": __import__("re").compile(r"^factory:(?:unowned|[0-9]+)$"),
        "json": json,
        "release_lease": lambda *args: False,
        "refresh_discovery_after_label_change": lambda *_args: None,
        **helpers,
    }
    exec(compile(module, "<factory-controller-release>", "exec"), namespace)
    return namespace["release_worker"]


def test_trusted_cleanup_fails_before_github_label_mutation_if_neon_unavailable() -> None:
    calls: list[int] = []

    def fail_snapshot(repo: str, worker: str):
        raise ConnectionError("Neon unavailable")

    release = _load_controller_release({
        "worker_claim": fail_snapshot,
        "list_issues": lambda: calls.append(1) or [],
    })
    with pytest.raises(ConnectionError, match="Neon unavailable"):
        release("104", "pre-session-abort-run-43439464262")
    assert calls == []


def test_post_session_cleanup_fails_closed_on_neon_read_outage() -> None:
    """A completed worker must not release GitHub labels without its Neon claim."""
    calls: list[int] = []

    def fail_snapshot(repo: str, worker: str):
        raise ConnectionError("Neon unavailable")

    release = _load_controller_release({
        "worker_claim": fail_snapshot,
        "list_issues": lambda: calls.append(1) or [],
    })
    with pytest.raises(ConnectionError, match="Neon unavailable"):
        release("45", "post-session-exit-run-43455638659")
    assert calls == []


def test_trusted_cleanup_retry_clears_original_claim_when_labels_already_released() -> None:
    releases: list[tuple[object, ...]] = []
    refreshed: list[int] = []
    release = _load_controller_release({
        "worker_claim": lambda repo, worker: ("old-claim-token", [4833, 5015]),
        "release_lease": lambda *args: releases.append(args) or True,
        "refresh_discovery_after_label_change":
            lambda number, target: refreshed.append(number),
    })
    assert release("45", "pre-session-abort-run-43450421076", expected_claim="old-claim-token") == []
    assert refreshed == [4833, 5015]
    assert releases == [
        ("owner/repo", "45", "old-claim-token", "pre-session-abort-run-43450421076")
    ]



def test_trusted_exit_releases_every_original_target_without_projection_scan() -> None:
    """Linked issue and PR are sourced from the UUID claim, not stale discovery."""
    labels = {4737: "factory:20", 5055: "factory:20"}
    released = []
    events = []
    token = ("claim-20", [4737, 5055])

    def target(number: int) -> dict:
        return {
            "labels": [{"name": labels[number]}],
            **({"pull_request": {}} if number == 5055 else {}),
        }

    def update(number: int, owner: str, *, expected_owner: str) -> bool:
        assert owner == "factory:unowned" and expected_owner == "factory:20"
        labels[number] = "factory:unowned"
        return True

    release = _load_controller_release({
        "worker_claim": lambda *args: token,
        "target_json": target,
        "replace_factory_labels": update,
        "target_owned_by": lambda number, owner: labels[number] == owner,
        "record_claim_released": lambda number, worker, kind, reason: events.append((number, kind)),
        "release_lease": lambda *args: released.append(args) or True,
        "list_issues": lambda: (_ for _ in ()).throw(AssertionError("unexpected inventory scan")),
        "list_prs": lambda: (_ for _ in ()).throw(AssertionError("unexpected inventory scan")),
    })
    summary = {}
    assert release("20", "pre-session-abort-run-43603288148", expected_claim="claim-20", details=summary) == [4737, 5055]
    assert summary["status"] == "released" and summary["claim_released"]
    assert released == [("owner/repo", "20", "claim-20", "pre-session-abort-run-43603288148")]
    assert events == [(4737, "issue"), (5055, "pr")]


def test_trusted_exit_no_claim_is_an_idempotent_noop() -> None:
    release = _load_controller_release({
        "worker_claim": lambda *args: None,
        "target_json": lambda *_: (_ for _ in ()).throw(AssertionError("unowned retry must not read")),
    })
    result = {}
    assert release("20", "post-session-exit-run-123", details=result) == []
    assert result["status"] == "already_released"
    assert result["claim_id"] is None


def test_trusted_exit_does_not_clear_reassigned_claim() -> None:
    """A new UUID between snapshot and mutation must leave both ledgers alone."""
    seen = 0
    writes = []
    leases = []

    def claim(*_args: object):
        nonlocal seen
        seen += 1
        return ("old", [5055]) if seen == 1 else ("new", [5055])

    release = _load_controller_release({
        "worker_claim": claim,
        "target_json": lambda *_: {"labels": [{"name": "factory:21"}], "pull_request": {}},
        "replace_factory_labels": lambda *a, **k: writes.append((a, k)),
        "release_lease": lambda *a: leases.append(a),
    })
    summary = {}
    assert release("21", "pre-session-abort-run-43604078425", expected_claim="old", details=summary) == []
    assert summary["status"] == "deferred"
    assert summary["deferred"] == [5055]
    assert not writes and not leases


def test_trusted_exit_reports_live_github_errors_instead_of_silent_success() -> None:
    release = _load_controller_release({
        "worker_claim": lambda *args: ("claim", [5055]),
        "target_json": lambda *_: (_ for _ in ()).throw(RuntimeError("github unavailable")),
        "release_lease": lambda *_: True,
    })
    summary = {}
    assert release("21", "post-session-exit-run-43604078425", expected_claim="claim", details=summary) == []
    # Owner direction (2026-11-17): the exited worker's exact Neon claim is
    # discharged even when GitHub fails, but the failure is never silent.
    assert summary["status"] == "labels_pending"
    assert summary["failed"] == ["5055:RuntimeError"]
    assert summary["labels_pending"] == [5055]


RATE_LIMITED = RuntimeError(
    "gh api repos/o/r/issues/5298 failed (1): gh: API rate limit exceeded for installation ID 1 (HTTP 403)"
)


def _owned_claim_targets(numbers: list[int], owner: str) -> dict[int, str]:
    return dict.fromkeys(numbers, owner)


def test_post_session_release_retries_installation_rate_limit() -> None:
    """Runs 44031401652/44032359658: a rate-limited label write stranded the lease."""
    labels = _owned_claim_targets([4843, 5298], "factory:7")
    failures = {"target_json": 2, "replace": 1}
    sleeps: list[float] = []
    released: list[tuple[object, ...]] = []

    def target(number: int) -> dict:
        if failures["target_json"]:
            failures["target_json"] -= 1
            raise RATE_LIMITED
        return {"labels": [{"name": labels[number]}],
                **({"pull_request": {}} if number == 5298 else {})}

    def update(number: int, owner: str, *, expected_owner: str) -> bool:
        if failures["replace"]:
            failures["replace"] -= 1
            raise RuntimeError("gh api failed (1): HTTP 502 Bad Gateway")
        labels[number] = owner
        return True

    release = _load_controller_release({
        "sleeps": sleeps,
        "worker_claim": lambda *args: ("claim-7", [4843, 5298]),
        "target_json": target,
        "replace_factory_labels": update,
        "target_owned_by": lambda number, owner: labels[number] == owner,
        "record_claim_released": lambda *args: None,
        "release_lease": lambda *args: released.append(args) or True,
    })
    summary: dict = {}
    assert release("7", "post-session-exit-run-44031401652", expected_claim="claim-7",
                   details=summary) == [4843, 5298]
    assert summary["status"] == "released" and summary["claim_released"]
    assert summary["failed"] == [] and summary["labels_pending"] == []
    assert sleeps == [2.0, 5.0, 2.0]
    assert released == [("owner/repo", "7", "claim-7", "post-session-exit-run-44031401652")]


def test_persistent_rate_limit_still_discharges_the_exact_neon_claim() -> None:
    """GitHub stays limited past the bounded backoff: free the claim, report labels."""
    sleeps: list[float] = []
    released: list[tuple[object, ...]] = []

    def target(number: int) -> dict:
        raise RATE_LIMITED

    release = _load_controller_release({
        "sleeps": sleeps,
        "worker_claim": lambda *args: ("claim-7", [4843, 5298]),
        "target_json": target,
        "release_lease": lambda *args: released.append(args) or True,
    })
    summary: dict = {}
    assert release("7", "post-session-exit-run-44032359658", expected_claim="claim-7",
                   details=summary) == []
    assert summary["status"] == "labels_pending"
    assert summary["claim_released"] is True
    assert summary["labels_pending"] == [4843, 5298]
    assert summary["failed"] == ["4843:RuntimeError", "5298:RuntimeError"]
    assert released == [("owner/repo", "7", "claim-7", "post-session-exit-run-44032359658")]
    assert sleeps == [2.0, 5.0, 15.0, 2.0, 5.0, 15.0]


def test_non_transient_github_errors_are_not_retried() -> None:
    sleeps: list[float] = []

    def target(number: int) -> dict:
        raise RuntimeError("gh api failed (1): HTTP 404 Not Found")

    release = _load_controller_release({
        "sleeps": sleeps,
        "worker_claim": lambda *args: ("claim-7", [4843]),
        "target_json": target,
        "release_lease": lambda *args: True,
    })
    summary: dict = {}
    release("7", "post-session-exit-run-1", expected_claim="claim-7", details=summary)
    assert sleeps == []
    assert summary["failed"] == ["4843:RuntimeError"]


def test_retried_label_write_that_already_landed_counts_as_released() -> None:
    labels = {4843: "factory:7"}
    sleeps: list[float] = []

    def update(number: int, owner: str, *, expected_owner: str) -> bool:
        if labels[number] != expected_owner:
            return False
        labels[number] = owner
        raise RuntimeError("gh api failed (1): HTTP 503 Service Unavailable")

    released: list[tuple[object, ...]] = []
    release = _load_controller_release({
        "sleeps": sleeps,
        "worker_claim": lambda *args: ("claim-7", [4843]),
        "target_json": lambda number: {"labels": [{"name": labels[number]}]},
        "replace_factory_labels": update,
        "target_owned_by": lambda number, owner: labels[number] == owner,
        "release_lease": lambda *args: released.append(args) or True,
    })
    summary: dict = {}
    release("7", "post-session-exit-run-1", expected_claim="claim-7", details=summary)
    assert summary["already_released"] == [4843]
    assert summary["status"] == "released"
    assert released
