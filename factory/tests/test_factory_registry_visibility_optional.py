"""Issue #1093 is retired as the factory heartbeat/registry.

GitHub refuses new comments on threads with more than 2,500 comments
(HTTP 403), and GitHub comments must not be operational infrastructure. Neon
and the trusted Actions job logs are authoritative. These tests prove that no
factory path reads, POSTs or PATCHes #1093 comments, that the lifecycle signal
the old read-back used to supply now comes from trusted Neon writes, and that
failover/transition evidence is still attested from the job log alone.

Worker IDs, sources and models are generated or discovered dynamically.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
WORKFLOWS = ROOT / ".github" / "workflows"
RUN_WORKFLOW = WORKFLOWS / "free-model-factory-run.yml"
ENTRY_WORKFLOW = WORKFLOWS / "free-model-factory-entry.yml"
OBSERVER_WORKFLOW = WORKFLOWS / "fixed-model-runtime-observer.yml"
RECOVERY_WORKFLOW = WORKFLOWS / "fixed-model-factory-dispatch-recovery.yml"
WORKER_SCRIPT = SCRIPTS / "free-model-factory-worker.sh"
PRIMITIVES = SCRIPTS / "free-model-factory-worker-primitives.sh"
SESSION_STEP = "Run continuous fixed-model factory session"
LIFECYCLE_START = "Record trusted worker start in Neon"
LIFECYCLE_EXIT = "Record trusted worker exit in Neon"

sys.path.insert(0, str(SCRIPTS))

# Every retired registry write or locator. Any of these in a factory path
# means a GitHub comment became operational infrastructure again.
RETIRED_REGISTRY = (
    re.compile(r"issues/(?:1093|\{TELEMETRY_ISSUE\})/comments"),
    re.compile(r"\bTELEMETRY_COMMENT_ID\b"),
    re.compile(r"\bTELEMETRY_ISSUE\b"),
    re.compile(r"\bFACTORY_HEARTBEAT_COMMENT_ID\b"),
    re.compile(r"\bFACTORY_ATTEMPT_COMMENT_ID\b"),
    re.compile(r"\bATTEMPT_COMMENT_ID\b"),
    re.compile(r"__heartbeat_id|__attempt_id"),
    re.compile(r"\bheartbeat_id\b|\battempt_id\b|\battempt_comment_id\b"),
    re.compile(r"factory_registry_ingest|factory_registry_verify"),
    re.compile(r"remember_verified_markers|record_heartbeat_comments|sync_registry\b"),
    re.compile(r"registry-comment-id-v1|registry-event-ingest"),
    re.compile(r"FACTORY_REGISTRY_VISIBILITY_SKIPPED|registry_visibility(?!_optional)"),
)

# Records the fake gh sees; any call at all is a regression for these paths.
FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["GH_CAPTURE"], "a", encoding="utf-8") as capture:
    capture.write(json.dumps(sys.argv[1:]) + "\n")
sys.stderr.write("gh: Commenting is disabled on issues with more than 2500 comments (HTTP 403)\n")
raise SystemExit(1)
"""


# Quoted, timestamped Actions log samples in classifier self-tests are
# historical data that the classifier must keep tolerating, not live paths.
LOG_SAMPLE = re.compile(r'^\s*\+?\s*f?"\d{4}-\d{2}-\d{2}T\S+Z ')


def _factory_sources() -> list[Path]:
    """Every executable factory path: workflows, actions and trusted scripts."""
    files = [
        *WORKFLOWS.glob("*.yml"),
        *(ROOT / ".github" / "actions").rglob("*.yml"),
        *SCRIPTS.glob("*.py"),
        *SCRIPTS.glob("*.sh"),
        *SCRIPTS.glob("*.cjs"),
        *SCRIPTS.glob("*.mjs"),
        *(ROOT / "scripts").glob("*.py"),
        *(ROOT / "scripts").glob("*.sh"),
    ]
    # In-tree unit tests may fake a legacy responder; they never run in Actions.
    return sorted(path for path in files if not path.name.startswith("test_"))


def _worker_id() -> str:
    return str(random.randint(10**11, 10**13))


def _workflow(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _step(workflow: Path, job: str, name: str) -> dict[str, Any]:
    data = _workflow(workflow)
    return next(step for step in data["jobs"][job]["steps"] if step.get("name") == name)


def _run_script(
    tmp_path: Path, script: str, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    capture = tmp_path / "gh-calls.jsonl"
    summary = tmp_path / "summary.md"
    summary.touch()
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GH_CAPTURE": str(capture),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_REPOSITORY": "JoshCLWren/comic-pile",
            "GITHUB_RUN_ID": str(random.randint(10**10, 10**11)),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    calls = (
        [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
        if capture.exists() else []
    )
    return result, calls


def test_no_factory_path_posts_or_patches_issue_1093_comments() -> None:
    """Regression: no workflow, action or trusted script touches #1093 comments."""
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
        for path in _factory_sources()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not LOG_SAMPLE.match(line)
        and any(pattern.search(line) for pattern in RETIRED_REGISTRY)
    ]
    assert offenders == []
    # A comment write to any issue from a factory path must name its target;
    # none may resolve to the retired registry through a variable.
    writes = re.compile(r"--method\s+(?:POST|PATCH)[^\n]*issues/[^\n]*comments")
    for path in _factory_sources():
        for line in path.read_text(encoding="utf-8").splitlines():
            if writes.search(line):
                assert "1093" not in line, f"{path.relative_to(ROOT)}: {line.strip()}"


def test_issue_1093_stays_protected_and_non_executable() -> None:
    """Retiring the registry never makes #1093 claimable or closable."""
    from factory_post_merge_closure import PROTECTED_ISSUES
    from factory_work_policy import NON_EXECUTABLE_ISSUES

    assert 1093 in NON_EXECUTABLE_ISSUES
    assert 1093 in PROTECTED_ISSUES


def test_session_steps_make_no_github_comment_calls(tmp_path: Path) -> None:
    """The run workflow's attempt summary needs no GitHub API at all."""
    step = _step(RUN_WORKFLOW, "factory", "Summarize factory attempt")
    assert step.get("continue-on-error") is True
    discovery = tmp_path / "discovery"
    discovery.write_text("model_unavailable\tobserved during real assigned work\n")
    result, calls = _run_script(tmp_path, str(step["run"]), {
        "WORKER": _worker_id(),
        "MODEL": f"model-{uuid.uuid4().hex[:8]}",
        "SOURCE": f"source-{uuid.uuid4().hex[:8]}",
        "CONFIGURED": "true",
        "REASON": "selected-from-live-runtime-catalog",
        "JOB_STATUS": "failure",
        "WORKER_STATUS_FILE": str(tmp_path / "missing-status"),
        "DISCOVERY_OUTCOME_FILE": str(discovery),
    })
    assert result.returncode == 0, result.stderr
    assert calls == []
    # Visibility goes to the job summary, never into the classified job log.
    assert "model_unavailable" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "model_unavailable" not in result.stdout + result.stderr


def test_session_cannot_be_gated_on_telemetry() -> None:
    """No pre-session step can stop the session; it is gated on the lane only."""
    steps = _workflow(RUN_WORKFLOW)["jobs"]["factory"]["steps"]
    session = next(i for i, step in enumerate(steps) if step.get("name") == SESSION_STEP)
    assert steps[session]["if"] == "steps.lane.outputs.configured == 'true'"
    for step in steps[:session]:
        assert "issues/" not in str(step.get("run") or "") or "comments" not in str(step.get("run") or "")


def test_lifecycle_heartbeat_moved_from_registry_read_back_to_trusted_neon() -> None:
    """The started/exit lifecycle the #1093 ingest supplied is written by trusted jobs."""
    data = _workflow(RUN_WORKFLOW)
    start = _step(RUN_WORKFLOW, "validate_assignment", LIFECYCLE_START)
    start_run = str(start["run"])
    assert "factory_state_ledger.py lifecycle" in start_run
    assert "--status started" in start_run
    assert '--run-id "$GITHUB_RUN_ID"' in start_run
    # Worker identity comes from the trusted dispatch input, never model output.
    assert start["env"]["FACTORY_WORKER"] == "${{ inputs.worker }}"
    # The DSN-bearing job is the trusted preparation job, not the model job.
    uses = [step.get("uses") for step in data["jobs"]["validate_assignment"]["steps"]]
    assert "./.github/actions/factory-state-cache" in uses
    factory_uses = [step.get("uses") for step in data["jobs"]["factory"]["steps"]]
    assert "./.github/actions/factory-state-cache" not in factory_uses
    for job in ("release_pre_session_lease", "release_post_session_lease"):
        step = _step(RUN_WORKFLOW, job, LIFECYCLE_EXIT)
        assert step["if"] == "always()"
        assert step["env"]["FACTORY_RESULT"] == "${{ needs.factory.result }}"
        assert step["env"]["FACTORY_WORKER"] == "${{ inputs.worker }}"
        assert "factory_state_ledger.py lifecycle" in str(step["run"])


@pytest.mark.parametrize(("status", "event_type", "shown"), [
    ("started", "worker_started", "running"),
    ("finished", "worker_outcome_signal", "finished"),
    ("failure", "worker_outcome_signal", "failure"),
])
def test_trusted_lifecycle_events_feed_the_runtime_view(
    monkeypatch: pytest.MonkeyPatch, status: str, event_type: str, shown: str,
) -> None:
    """Lifecycle events project into the same heartbeat view recovery reads."""
    import factory_state_ledger as ledger
    from factory_runtime_view import project_events

    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(
        ledger, "record_events",
        lambda _repo, entries: recorded.extend(entries) or len(entries),
    )
    worker, run_id = _worker_id(), str(random.randint(10**10, 10**11))
    assert ledger.record_lifecycle("owner/repo", run_id, worker, status) == 1
    (event,) = recorded
    assert event["event_type"] == event_type
    assert event["event_id"] == f"lifecycle:{run_id}:{status}"
    view = project_events([{"event_type": event_type, "detail": event, "created_at": ""}])
    assert view["heartbeats"][worker]["run"] == run_id
    assert view["heartbeats"][worker]["outcome"] == shown
    with pytest.raises(ValueError):
        ledger.record_lifecycle("owner/repo", run_id, worker, "running")
    with pytest.raises(ValueError):
        ledger.record_lifecycle("owner/repo", "not-a-run", worker, status)


def test_dispatch_recovery_reads_only_neon() -> None:
    """Recovery freshness has no GitHub comment fallback any more."""
    workflow = RECOVERY_WORKFLOW.read_text(encoding="utf-8")
    assert "from factory_runtime_view import export_view" in workflow
    assert "comments" not in workflow
    assert "issues:" not in str(_workflow(RECOVERY_WORKFLOW).get("permissions"))


def test_runtime_observer_persists_to_neon_without_comments() -> None:
    """The observer's only durable write is the Neon runtime outcome."""
    data = _workflow(OBSERVER_WORKFLOW)
    steps = data["jobs"]["publish-runtime-outcome"]["steps"]
    for step in steps:
        assert "comments" not in str(step.get("run") or ""), step.get("name")
    persist = next(s for s in steps if s.get("name") == "Persist classified worker completion in Neon")
    assert str(persist["if"]).startswith("always()")
    assert data["permissions"]["issues"] == "read"


def test_entry_persists_outcome_without_registry_patch() -> None:
    """Entry's terminal verdict is Neon plus job summary only."""
    workflow = ENTRY_WORKFLOW.read_text(encoding="utf-8")
    assert "factory_state_ledger.py runtime-outcome" in workflow
    assert "issues/comments" not in workflow


def _worker_failover_harness(tmp_path: Path, candidates: list[dict[str, str]]) -> str:
    """Build a bash harness around the real worker failover functions."""
    worker = WORKER_SCRIPT.read_text(encoding="utf-8")
    start = worker.index("record_model_failover_failure() {")
    end = worker.index("\n}\n", worker.index("select_next_eligible_model() {")) + 3
    functions = worker[start:end]
    log_line = next(
        line for line in PRIMITIVES.read_text(encoding="utf-8").splitlines()
        if line.startswith("log() {")
    )
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "candidates.json").write_text(json.dumps(candidates))
    # Fake trusted selector: returns the first candidate not yet attempted.
    (trusted / "factory_model_failover.py").write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "tried = {args[i + 1] for i, a in enumerate(args) if a == '--attempted-pair'}\n"
        "for c in json.loads((pathlib.Path(__file__).parent / 'candidates.json').read_text()):\n"
        "    if json.dumps([c['source'], c['model']], separators=(',', ':')) not in tried:\n"
        "        print(json.dumps(c)); raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    (trusted / "factory_provider_registry.py").write_text("print('{}')\n")
    # Exit 3 marks the first runtime as missing from the executable catalog.
    unavailable = candidates[0]["runtime_model"]
    (trusted / "wait-opencode-model.sh").write_text(
        f'[[ "$1" == "{unavailable}" ]] && exit 3\nexit 0\n'
    )
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text("{}")
    return f"""
set -Eeuo pipefail
export HOME={home}
FACTORY_TRUSTED_FAILOVER_DIR={trusted}
TERMINAL_OUTCOME_FILE={tmp_path}/terminal-outcome
{log_line}
remaining() {{ echo 100000; }}
stop_work_checkpoint() {{ :; }}
start_work_checkpoint() {{ :; }}
record_terminal_outcome() {{ printf '%s\\t%s\\n' "$1" "$2" > "$TERMINAL_OUTCOME_FILE"; }}
{functions}
ORIGINAL_SOURCE="$SOURCE"
attempted_pairs=()
blocked_providers=()
# Same call the worker makes after an attributable inference failure.
if ! record_model_failover_failure "$SOURCE" "$MODEL" provider_throttle model; then
  echo 'control-plane stop' >&2
  exit 9
fi
select_next_eligible_model
echo "SELECTED source=$SOURCE model=$MODEL runtime=$RUNTIME_MODEL"
"""


def test_worker_failover_is_attested_from_the_log_without_any_gh_call(tmp_path: Path) -> None:
    """Failover/transition evidence reaches Neon from the job log alone."""
    from factory_provider_candidates import ADAPTERS
    from factory_state_ledger import refine_trusted_failover_models

    # Sources whose executable catalog is not pre-checked cannot model a
    # catalog miss; read that exemption from the selector instead of naming it.
    selector = WORKER_SCRIPT.read_text(encoding="utf-8").split("select_next_eligible_model() {", 1)[1]
    exempt = set(re.findall(
        r'if \[\[ "\$next_source" != (\S+) \]\]; then\n\s+candidate_status=0', selector,
    ))
    assert exempt
    sources = sorted(set(ADAPTERS) - exempt)
    assert len(sources) >= 3
    origin, first, second = random.Random().sample(sources, 3)
    candidates = [
        {"source": src, "model": f"m-{uuid.uuid4().hex[:10]}", "runtime_model": f"rt-{uuid.uuid4().hex[:10]}"}
        for src in (first, second)
    ]
    worker = _worker_id()
    origin_model = f"m-{uuid.uuid4().hex[:10]}"
    script = _worker_failover_harness(tmp_path, candidates)
    result, calls = _run_script(tmp_path, script, {
        "WORKER": worker, "WORKER_ID": f"opencode-free-model-factory-{worker}",
        "SOURCE": origin, "MODEL": origin_model, "RUNTIME_MODEL": "rt-origin",
        "MODE": "issue", "NUMBER": str(random.randint(100, 9999)), "EXPECTED_HEAD": "",
        "GITHUB_RUN_NUMBER": "1",
    })
    assert result.returncode == 0, result.stderr
    assert calls == []
    chosen = candidates[1]
    assert (
        f"SELECTED source={chosen['source']} model={chosen['model']} "
        f"runtime={chosen['runtime_model']}"
    ) in result.stdout
    assert "FACTORY_FLEET_TRANSITION:" in result.stdout
    assert not (tmp_path / "terminal-outcome").exists()

    # Actions timestamps every log line; the trusted persister reads only that.
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    job_log = "\n".join(f"{stamp} {line}" for line in result.stdout.splitlines())
    refined = refine_trusted_failover_models(
        {"worker": worker, "source": chosen["source"], "model": chosen["model"]}, job_log,
    )
    assert refined["trusted_source_switch"] is True
    # The attributable inference failure keeps independent, model-scoped health.
    assert {
        "source": origin, "model": origin_model,
        "outcome": "provider_throttle", "scope": "model",
    } in refined["model_failures"]


def test_lease_validation_remains_fatal() -> None:
    """Only the lifecycle observation is advisory; assignment validation gates the job."""
    data = _workflow(RUN_WORKFLOW)
    factory = data["jobs"]["factory"]
    assert factory["needs"] == "validate_assignment"
    assert "always()" not in str(factory.get("if") or "")
    for step in data["jobs"]["validate_assignment"]["steps"]:
        if step.get("name") == LIFECYCLE_START:
            continue
        assert step.get("continue-on-error") is not True, step.get("name")
    for name in ("Verify original active claim and live target", "Require trusted push credential"):
        steps = [s for job in data["jobs"].values() for s in job["steps"] if s.get("name") == name]
        assert steps and all(s.get("continue-on-error") is not True for s in steps)
