"""Regression coverage for the worker-direct canonical merge and closure route.

Issue #4081: the non-``kilo-auto`` worker merge route used to merge on a reduced
gate (mergeability, SHA, required checks, review threads) and then close the
linked issue with a raw ``gh issue close``. These tests execute the real route
and the real primitives call site, not just the standalone dependency parser.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
LIBRARY = SCRIPTS / "factory-canonical-merge-route.sh"
PRIMITIVES = SCRIPTS / "free-model-factory-worker-primitives.sh"

PR = 4085
HEAD = "b65ac536217a64c10071ff8e8371571491d153b8"

CONTROLLER_BODY = """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as handle:
    handle.write("gate " + os.path.abspath(__file__) + " " + " ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_GATE_EXIT"):
    print("controller exploded")
    raise SystemExit(int(os.environ["FAKE_GATE_EXIT"]))
if os.environ.get("FAKE_GATE_MALFORMED") == "1":
    print("not-json")
else:
    print(json.dumps({
        "decision": os.environ.get("FAKE_GATE_DECISION", "pass"),
        "reason": os.environ.get("FAKE_GATE_REASON", "all exact-head mechanical gates passed"),
    }))
"""

CLOSURE_BODY = """#!/usr/bin/env python3
import os
import sys

with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(
        "closure " + os.path.abspath(__file__) + " " + " ".join(sys.argv[1:]) + "\\n"
    )
if os.environ.get("FAKE_CLOSURE_ERROR") == "1":
    print("RuntimeError: closure API read failed")
    raise SystemExit(1)
print(os.environ.get("FAKE_CLOSURE_OUTCOME", "closed"))
"""

GH_BODY = """#!/usr/bin/env bash
printf 'gh %s\\n' "$*" >> "${CALL_LOG:?}"
if [[ "${1:-} ${2:-}" == "pr merge" ]]; then
  exit "${FAKE_MERGE_EXIT:-0}"
fi
exit 0
"""


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _write_fake_controller(trusted: Path) -> None:
    (trusted / "factory-review-controller.py").write_text(CONTROLLER_BODY, encoding="utf-8")


def _write_fake_closure(trusted: Path) -> None:
    (trusted / "factory_post_merge_closure.py").write_text(CLOSURE_BODY, encoding="utf-8")


def _fixture(
    tmp_path: Path,
    *,
    with_controller: bool = True,
    with_closure: bool = True,
) -> tuple[Path, dict[str, str]]:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(bin_dir / "gh", GH_BODY)
    if with_controller:
        _write_fake_controller(trusted)
    if with_closure:
        _write_fake_closure(trusted)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CALL_LOG": str(tmp_path / "calls.log"),
        "TRUSTED_REVIEW_CONTROLLER": str(trusted / "factory-review-controller.py"),
        "FAKE_GATE_DECISION": "pass",
        "FAKE_GATE_REASON": "all exact-head mechanical gates passed",
        "FAKE_MERGE_EXIT": "0",
        "FAKE_CLOSURE_OUTCOME": "closed",
    }
    return tmp_path, env


def _run_route(tmp_path: Path, **env_overrides: str) -> dict[str, str]:
    _, env = _fixture(tmp_path)
    env.update(env_overrides)
    script = f"""
set -Eeuo pipefail
source {shlex.quote(str(LIBRARY))}
factory_dispatch_production_deploy() {{ printf 'dispatch %s\\n' "$*" >> "$CALL_LOG"; }}
set +e
factory_merge_with_canonical_gates {PR} {HEAD}
status=$?
set -e
printf 'status=%s\\n' "$status"
printf 'state=%s\\n' "${{FACTORY_MERGE_STATE:-}}"
printf 'decision=%s\\n' "${{FACTORY_MERGE_GATE_DECISION:-}}"
printf 'reason=%s\\n' "${{FACTORY_MERGE_GATE_REASON:-}}"
printf 'closure=%s\\n' "${{FACTORY_CLOSURE_OUTCOME:-}}"
printf 'closure_status=%s\\n' "${{FACTORY_CLOSURE_STATUS:-}}"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    fields = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    fields["calls"] = _calls(tmp_path)
    return fields


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def _merge_calls(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("gh pr merge")]


def _closure_calls(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("closure ")]


def _gate_calls(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("gate ")]


def test_open_declared_dependency_blocks_the_worker_direct_merge(tmp_path: Path) -> None:
    """A green/approved PR with an open declared dependency never merges."""
    fields = _run_route(
        tmp_path,
        FAKE_GATE_DECISION="retry",
        FAKE_GATE_REASON="declared dependencies still open: #4023, #4026",
    )
    assert fields["status"] == "1"
    assert fields["state"] == "denied"
    assert fields["decision"] == "retry"
    assert "#4023" in fields["reason"]
    assert _gate_calls(fields["calls"]), "the trusted controller was not consulted"
    assert _merge_calls(fields["calls"]) == []
    assert _closure_calls(fields["calls"]) == []


def test_retry_decision_exit_zero_does_not_merge(tmp_path: Path) -> None:
    """Retry decisions exit zero, so the JSON decision must be authoritative."""
    fields = _run_route(
        tmp_path,
        FAKE_GATE_DECISION="retry",
        FAKE_GATE_REASON="required checks are not terminal: IN_PROGRESS",
    )
    assert fields["status"] == "1"
    assert fields["decision"] == "retry"
    assert _merge_calls(fields["calls"]) == []


def test_deny_decision_does_not_merge(tmp_path: Path) -> None:
    """A deny decision never enters the merge route."""
    fields = _run_route(
        tmp_path,
        FAKE_GATE_DECISION="deny",
        FAKE_GATE_REASON="linkage reversal: #4027 with open declared dependencies",
    )
    assert fields["status"] == "1"
    assert fields["decision"] == "deny"
    assert fields["reason"] == "linkage reversal: #4027 with open declared dependencies"
    assert fields["state"] == "denied"
    assert _merge_calls(fields["calls"]) == []
    assert _closure_calls(fields["calls"]) == []


@pytest.mark.parametrize(
    "reason",
    [
        "pull request head moved after acceptance was recorded",
        "dependency state unavailable for #4023",
        "linked issue body could not be read",
    ],
)
def test_changed_head_or_unreadable_dependency_fails_closed(
    tmp_path: Path, reason: str
) -> None:
    """A moved head or unreadable dependency can never become a merge."""
    fields = _run_route(
        tmp_path,
        FAKE_GATE_DECISION="retry",
        FAKE_GATE_REASON=reason,
    )
    assert fields["status"] == "1"
    assert reason in fields["reason"]
    assert fields["state"] == "denied"
    assert _merge_calls(fields["calls"]) == []


def test_unreadable_gate_payload_fails_closed(tmp_path: Path) -> None:
    """Missing or malformed controller JSON never becomes a merge."""
    for index, overrides in enumerate(
        (
            {"FAKE_GATE_MALFORMED": "1"},
            {"FAKE_GATE_EXIT": "1"},
        )
    ):
        case_dir = tmp_path / str(index)
        case_dir.mkdir(parents=True)
        fields = _run_route(case_dir, **overrides)
        assert fields["status"] == "1", fields
        assert fields["decision"] == "", fields
        assert _merge_calls(fields["calls"]) == [], fields


def test_missing_trusted_controller_fails_closed(tmp_path: Path) -> None:
    """A worker that cannot prove the trusted bundle never merges."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(bin_dir / "gh", GH_BODY)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CALL_LOG": str(tmp_path / "calls.log"),
        "TRUSTED_REVIEW_CONTROLLER": "",
    }
    script = f"""
set -Eeuo pipefail
source {shlex.quote(str(LIBRARY))}
factory_dispatch_production_deploy() {{ printf 'dispatch %s\\n' "$*" >> "$CALL_LOG"; }}
set +e
factory_merge_with_canonical_gates {PR} {HEAD}
status=$?
set -e
printf 'status=%s\\n' "$status"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=1" in result.stdout
    assert _merge_calls(_calls(tmp_path)) == []


def test_missing_trusted_closure_module_fails_closed_before_merge(tmp_path: Path) -> None:
    """Closure policy is trusted main code; without it the merge must not happen."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(bin_dir / "gh", GH_BODY)
    _write_fake_controller(trusted)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CALL_LOG": str(tmp_path / "calls.log"),
        "TRUSTED_REVIEW_CONTROLLER": str(trusted / "factory-review-controller.py"),
        "FAKE_GATE_DECISION": "pass",
    }
    script = f"""
set -Eeuo pipefail
source {shlex.quote(str(LIBRARY))}
factory_dispatch_production_deploy() {{ printf 'dispatch %s\\n' "$*" >> "$CALL_LOG"; }}
set +e
factory_merge_with_canonical_gates {PR} {HEAD}
status=$?
set -e
printf 'status=%s\\n' "$status"
printf 'reason=%s\\n' "${{FACTORY_MERGE_GATE_REASON:-}}"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=1" in result.stdout
    assert "closure module is not staged" in result.stdout
    assert _merge_calls(_calls(tmp_path)) == []


def test_passing_gates_merge_one_exact_head_and_use_trusted_closure(tmp_path: Path) -> None:
    """Dependency-free work merges once, then the trusted closure runs once."""
    fields = _run_route(tmp_path)
    assert fields["status"] == "0"
    assert fields["state"] == "merged"
    assert fields["decision"] == "pass"
    assert fields["closure_status"] == "closed"
    merges = _merge_calls(fields["calls"])
    assert merges == [
        f"gh pr merge {PR} --merge --match-head-commit {HEAD} --delete-branch"
    ]
    closures = _closure_calls(fields["calls"])
    assert len(closures) == 1
    assert str(tmp_path / "trusted" / "factory_post_merge_closure.py") in closures[0]
    assert f"--pr {PR}" in closures[0]
    gates = _gate_calls(fields["calls"])
    assert len(gates) == 1
    assert str(tmp_path / "trusted" / "factory-review-controller.py") in gates[0]
    assert gates[0].endswith(f"gates --pr {PR} --expected-head {HEAD}")
    assert [call for call in fields["calls"] if call.startswith("dispatch ")] == [
        f"dispatch Factory worker merged PR #{PR}"
    ]
    assert not [call for call in fields["calls"] if call.startswith("gh issue close")]


def test_docs_only_or_dependency_free_work_can_still_merge(tmp_path: Path) -> None:
    """Canonical gates plus no linked issue still completes the merge route."""
    fields = _run_route(tmp_path, FAKE_CLOSURE_OUTCOME="no-linked-issue")
    assert fields["status"] == "0"
    assert fields["state"] == "merged"
    assert fields["closure_status"] == "not-applicable"
    assert len(_merge_calls(fields["calls"])) == 1


def test_blocked_closure_leaves_issue_open_and_never_marks_done(tmp_path: Path) -> None:
    """A blocked closure is reported, not swallowed and never treated as done."""
    fields = _run_route(tmp_path, FAKE_CLOSURE_OUTCOME="open-dependency: #4016")
    assert fields["status"] == "3"
    assert fields["state"] == "merged"
    assert fields["closure_status"] == "blocked"
    assert fields["closure"] == "open-dependency: #4016"
    assert len(_closure_calls(fields["calls"])) == 1
    assert not [call for call in fields["calls"] if call.startswith("gh issue close")]
    assert not [call for call in fields["calls"] if "ralph-status:done" in call]


def test_unknown_or_failed_closure_is_reported_as_error(tmp_path: Path) -> None:
    """Unknown or failed closure outcomes remain errors, not acceptance."""
    for index, overrides in enumerate(
        (
            {"FAKE_CLOSURE_ERROR": "1"},
            {"FAKE_CLOSURE_OUTCOME": "not-delivered: docs-only"},
            {"FAKE_CLOSURE_OUTCOME": "acceptance-parent-requires-verdict"},
        )
    ):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        fields = _run_route(case_dir, **overrides)
        assert fields["status"] == "3", fields
        assert fields["state"] == "merged"
        assert fields["closure_status"] in {"blocked", "error"}
        assert not [call for call in fields["calls"] if call.startswith("gh issue close")]


def test_merge_command_failure_does_not_run_closure(tmp_path: Path) -> None:
    """A failed exact-head merge never invokes issue closure."""
    fields = _run_route(tmp_path, FAKE_MERGE_EXIT="1")
    assert fields["status"] == "2"
    assert fields["state"] == "merge-failed"
    assert _merge_calls(fields["calls"])
    assert _closure_calls(fields["calls"]) == []


def _worker_merge_call_site() -> str:
    source = PRIMITIVES.read_text(encoding="utf-8")
    start = source.index('  current="$(git rev-parse HEAD)"')
    end = source.index("  if (( transient_failure == 1 )); then", start)
    return source[start:end]


def _run_call_site(tmp_path: Path, **env_overrides: str) -> tuple[list[str], str]:
    _, env = _fixture(tmp_path)
    env.update(env_overrides)
    worker = "mergeroute" + str(os.getpid())
    worker_log = Path("/tmp") / f"opencode-factory-{worker}.log"
    worker_log.write_text("FACTORY_GATE_READY\n", encoding="utf-8")
    block = _worker_merge_call_site().replace(
        'current="$(git rev-parse HEAD)"', 'current="$EXPECTED_HEAD"', 1
    )
    script = f"""
set -Eeuo pipefail
source {shlex.quote(str(LIBRARY))}
SOURCE=opencode-free
NUMBER={PR}
BRANCH=factory/87-4081-opencode-free
WORKER={worker}
MODEL=test-model
EXPECTED_HEAD={HEAD}

log() {{ printf 'log %s\\n' "$*" >> "$CALL_LOG"; }}
release_pr_and_issue() {{ printf 'release %s\\n' "$*" >> "$CALL_LOG"; }}
factory_dispatch_production_deploy() {{ printf 'dispatch %s\\n' "$*" >> "$CALL_LOG"; }}
SKIP_PRS=()
run_once=0
while (( run_once == 0 )); do
  run_once=1
{block}
done
printf 'after-block\\n' >> "$CALL_LOG"
# A blocked PR must not be handed straight back to this same session.
printf 'skip %s\\n' "${{SKIP_PRS[*]:-}}" >> "$CALL_LOG"
"""
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    finally:
        worker_log.unlink(missing_ok=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return _calls(tmp_path), result.stdout


def test_primitives_call_site_runs_the_canonical_route_and_never_raw_closes() -> None:
    """The actual worker shell call site, not a standalone mock."""
    block = _worker_merge_call_site()
    assert "factory_merge_with_canonical_gates" in block
    assert "gh issue close" not in block
    assert "machine_merge_gates_pass" not in block
    assert "stage_trusted_review_controller" not in block


def test_worker_call_site_gate_denial_releases_without_merging(tmp_path: Path) -> None:
    """A denied worker PR is released and skipped instead of retried inline."""
    calls, _ = _run_call_site(tmp_path, FAKE_GATE_DECISION="retry")
    assert _merge_calls(calls) == []
    assert _closure_calls(calls) == []
    assert [call for call in calls if call.startswith("release ")] == [
        f"release {PR} factory/87-4081-opencode-free factory:review gate-denied: all exact-head mechanical gates passed"
    ]
    assert any("decision=retry" in call for call in calls if call.startswith("log "))
    # Pending CI reaches this branch on every attempt; the released lease must
    # not let choose_unowned_pr hand the same unchanged PR back in this session.
    assert f"skip {PR}" in calls


def test_worker_call_site_deny_decision_releases_as_blocked_before_merge(tmp_path: Path) -> None:
    """A deny decision releases the PR as factory:blocked so it won't get repeated work."""
    calls, _ = _run_call_site(tmp_path, FAKE_GATE_DECISION="deny", FAKE_GATE_REASON="linkage reversal: #4027")
    assert _merge_calls(calls) == []
    assert _closure_calls(calls) == []
    assert [call for call in calls if call.startswith("release ")] == [
        f"release {PR} factory/87-4081-opencode-free factory:blocked gate-denied: linkage reversal: #4027"
    ]
    assert any("decision=deny" in call for call in calls if call.startswith("log "))
    # A denied PR must be blocked to prevent the session from handing it straight back.
    assert f"skip {PR}" in calls


def test_worker_call_site_merges_then_closes_through_trusted_policy(tmp_path: Path) -> None:
    """The worker merges through exact-head gates and trusted closure."""
    calls, _ = _run_call_site(tmp_path)
    assert _merge_calls(calls) == [
        f"gh pr merge {PR} --merge --match-head-commit {HEAD} --delete-branch"
    ]
    assert len(_closure_calls(calls)) == 1
    assert not [call for call in calls if call.startswith("gh issue close")]
    assert not [call for call in calls if call.startswith("release ")]
    assert any("post-merge closure closed" in call for call in calls if call.startswith("log "))
    assert calls[-1] == "skip "


def test_worker_call_site_blocked_closure_does_not_report_acceptance(tmp_path: Path) -> None:
    """A blocked closure leaves its issue open and never reports acceptance."""
    calls, _ = _run_call_site(tmp_path, FAKE_CLOSURE_OUTCOME="open-dependency: #4016")
    assert len(_merge_calls(calls)) == 1
    assert any("issue left open" in call for call in calls if call.startswith("log "))
    assert any("open-dependency: #4016" in call for call in calls if call.startswith("log "))
    assert not [call for call in calls if "ralph-status:done" in call]
    assert not [call for call in calls if call.startswith("gh issue close")]
    assert f"skip {PR}" in calls


def test_worker_call_site_merge_failure_does_not_repeat_the_same_pr(tmp_path: Path) -> None:
    """A failed merge leaves the PR leased, so release it and skip it instead of re-running it."""
    calls, _ = _run_call_site(tmp_path, FAKE_MERGE_EXIT="1")
    assert _merge_calls(calls)
    assert _closure_calls(calls) == []
    # Release the lease before skipping so a peer can retry without waiting
    # for this session to end (Greptile P1: failed merges stay reserved).
    assert [call for call in calls if call.startswith("release ")] == [
        f"release {PR} factory/87-4081-opencode-free factory:review merge-failed"
    ]
    assert f"skip {PR}" in calls


NVIDIA_WORKER = SCRIPTS / "nvidia-factory-worker.sh"
OMNIROUTE_WORKER = SCRIPTS / "omniroute-factory-worker.sh"
LEGACY_WORKERS = pytest.mark.parametrize(
    "worker_path", [NVIDIA_WORKER, OMNIROUTE_WORKER], ids=["nvidia", "omniroute"]
)


def _legacy_worker_merge_block(worker_path: Path) -> str:
    """Extract the real merge call site from a legacy worker script."""
    source = worker_path.read_text(encoding="utf-8")
    start = source.index('  current="$(git rev-parse HEAD)"')
    end = source.index("  if (( transient_failure == 1 )); then", start)
    return source[start:end]


def _run_legacy_call_site(
    tmp_path: Path, worker_path: Path, **env_overrides: str
) -> tuple[list[str], str]:
    """Execute one legacy worker's real merge block with fakes and stubs."""
    _, env = _fixture(tmp_path)
    env.update(env_overrides)
    branch_suffix = worker_path.stem.split("-")[0]
    worker = "legacy" + str(os.getpid())
    worker_log = Path("/tmp") / f"opencode-factory-{worker}.log"
    worker_log.write_text("FACTORY_GATE_READY\n", encoding="utf-8")
    block = _legacy_worker_merge_block(worker_path).replace(
        'current="$(git rev-parse HEAD)"', 'current="$EXPECTED_HEAD"', 1
    )
    script = f"""
set -Eeuo pipefail
source {shlex.quote(str(LIBRARY))}
NUMBER={PR}
BRANCH=factory/87-4081-{branch_suffix}
WORKER={worker}
MODEL=test-model
CALL_SIGN=test-sign
EXPECTED_HEAD={HEAD}
transient_failure=0

log() {{ printf 'log %s\\n' "$*" >> "$CALL_LOG"; }}
release_pr_and_issue() {{ printf 'release %s\\n' "$*" >> "$CALL_LOG"; }}
factory_dispatch_production_deploy() {{ printf 'dispatch %s\\n' "$*" >> "$CALL_LOG"; }}
SKIP_PRS=()
run_once=0
while (( run_once == 0 )); do
  run_once=1
{block}
done
printf 'after-block\\n' >> "$CALL_LOG"
printf 'skip %s\\n' "${{SKIP_PRS[*]:-}}" >> "$CALL_LOG"
"""
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    finally:
        worker_log.unlink(missing_ok=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return _calls(tmp_path), result.stdout


@LEGACY_WORKERS
def test_legacy_worker_routes_through_canonical_gates(
    tmp_path: Path, worker_path: Path
) -> None:
    """Legacy workers use the canonical route, never a raw merge plus close."""
    block = _legacy_worker_merge_block(worker_path)
    assert "factory_merge_with_canonical_gates" in block
    assert "gh issue close" not in block
    assert "gh pr merge" not in block
    assert "machine_merge_gates_pass" not in block
    assert "release_pr_and_issue" in block
    assert "SKIP_PRS+=$NUMBER" not in block or "SKIP_PRS+=(\"$NUMBER\")" in block


@LEGACY_WORKERS
def test_legacy_worker_gate_denial_releases_and_skips(
    tmp_path: Path, worker_path: Path
) -> None:
    """A retry/deny decision releases the PR and issue instead of looping."""
    calls, _ = _run_legacy_call_site(
        tmp_path, worker_path, FAKE_GATE_DECISION="retry"
    )
    assert _merge_calls(calls) == []
    assert _closure_calls(calls) == []
    branch_suffix = worker_path.stem.split("-")[0]
    assert [c for c in calls if c.startswith("release ")] == [
        f"release {PR} factory/87-4081-{branch_suffix} factory:review gate-denied"
    ]
    assert f"skip {PR}" in calls


@LEGACY_WORKERS
def test_legacy_worker_merges_and_closes_through_trusted_policy(
    tmp_path: Path, worker_path: Path
) -> None:
    """Passing gates merge once and close through the trusted closure policy."""
    calls, _ = _run_legacy_call_site(tmp_path, worker_path)
    assert _merge_calls(calls) == [
        f"gh pr merge {PR} --merge --match-head-commit {HEAD} --delete-branch"
    ]
    assert len(_closure_calls(calls)) == 1
    assert not [c for c in calls if c.startswith("gh issue close")]
    assert not [c for c in calls if c.startswith("release ")]
    assert any("post-merge closure closed" in c for c in calls if c.startswith("log "))
    assert calls[-1] == "skip "


@LEGACY_WORKERS
def test_legacy_worker_blocked_closure_does_not_report_acceptance(
    tmp_path: Path, worker_path: Path
) -> None:
    """A blocked closure is logged as left-open, never as accepted or closed."""
    calls, _ = _run_legacy_call_site(
        tmp_path, worker_path, FAKE_CLOSURE_OUTCOME="open-dependency: #4016"
    )
    assert len(_merge_calls(calls)) == 1
    assert any(
        "issue left open" in c for c in calls if c.startswith("log ")
    )
    assert not [c for c in calls if c.startswith("gh issue close")]
    assert not [c for c in calls if "ralph-status:done" in c]
    assert f"skip {PR}" in calls


@LEGACY_WORKERS
def test_legacy_worker_merge_failure_releases_and_skips(
    tmp_path: Path, worker_path: Path
) -> None:
    """A failed merge releases the lease instead of keeping the PR reserved."""
    calls, _ = _run_legacy_call_site(tmp_path, worker_path, FAKE_MERGE_EXIT="1")
    assert _merge_calls(calls)
    assert _closure_calls(calls) == []
    branch_suffix = worker_path.stem.split("-")[0]
    assert [c for c in calls if c.startswith("release ")] == [
        f"release {PR} factory/87-4081-{branch_suffix} factory:review merge-failed"
    ]
    assert f"skip {PR}" in calls
