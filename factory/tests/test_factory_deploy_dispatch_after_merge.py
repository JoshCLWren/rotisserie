"""Every factory bot-merge path dispatches Deploy Production.

A merge made with the built-in GITHUB_TOKEN fires no push or workflow_run
event, so Deploy Production never ran for factory merges (PR #3869 merged as
1319bc5a at 12:10 PM CT with no deploy). workflow_dispatch is the one event
GITHUB_TOKEN may raise, so every bot-merge path must call
``factory_dispatch_production_deploy`` and run with ``actions: write``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".github/scripts/factory-dispatch-production-deploy.sh"
DISPATCH_CALL = "factory_dispatch_production_deploy"

MERGE_RE = re.compile(
    r"gh pr merge\b|/pulls/[^\s\"']*/merge\b|pulls\.merge\(|mergePullRequest|enablePullRequestAutoMerge"
)
# Merge paths that do not merge with GITHUB_TOKEN, with the reason.
EXEMPT = {
    # Local operator pipeline: merges with the operator's own token, which
    # fires push events, so Deploy Production runs on its own.
    "scripts/opencode_pipeline.sh",
    # Validator that forbids the merge string in the live worker.
    ".github/scripts/validate-free-model-factories.py",
}
SCANNED_SUFFIXES = {".sh", ".py", ".cjs", ".mjs", ".js", ".yml", ".yaml"}


def code_lines(path: Path) -> list[tuple[int, str]]:
    lines = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*")):
            continue
        lines.append((number, line))
    return lines


def bot_merge_paths() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for base in (ROOT / ".github", ROOT / "scripts"):
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in f"/{rel}" or rel.endswith((".test.cjs", ".test.mjs", ".test.js")):
                continue
            hits = [n for n, line in code_lines(path) if MERGE_RE.search(line)]
            if hits:
                found[rel] = hits
    return found


def test_known_bot_merge_paths_are_all_found() -> None:
    """Find concrete merge call sites; legacy workers delegate to the shared route."""
    paths = set(bot_merge_paths())
    # The legacy NVIDIA and Omniroute workers now delegate to the canonical
    # merge route; their call sites are covered in test_factory_worker_merge_route.py.
    assert {
        ".github/workflows/factory-ready-merge-drain.yml",
        ".github/scripts/factory-canonical-merge-route.sh",
    } <= paths
    # Merges are serialized: the dispatcher only triggers the drain.
    assert ".github/workflows/fixed-model-factory-dispatch.yml" not in paths


def test_every_bot_merge_path_dispatches_deploy_production() -> None:
    missing = [
        rel
        for rel in bot_merge_paths()
        if rel not in EXEMPT and DISPATCH_CALL not in (ROOT / rel).read_text(encoding="utf-8")
    ]
    assert not missing, (
        "These files merge PRs but never dispatch Deploy Production; a GITHUB_TOKEN merge "
        f"fires no push event, so call {DISPATCH_CALL} after the merge: {missing}"
    )


def test_worker_merges_dispatch_right_after_the_merge() -> None:
    for rel, hits in bot_merge_paths().items():
        if rel in EXEMPT or not rel.endswith(".sh"):
            continue
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        for number in hits:
            following = "\n".join(lines[number : number + 3])
            assert DISPATCH_CALL in following, (
                f"{rel}:{number} merges without dispatching the deploy"
            )


def _effective_actions_permission(workflow: dict, job: dict) -> str | None:
    perms = job.get("permissions", workflow.get("permissions"))
    if perms == "write-all":
        return "write"
    if isinstance(perms, dict):
        return perms.get("actions")
    return None


def test_bot_merge_workflow_jobs_can_dispatch_workflows() -> None:
    for rel in bot_merge_paths():
        if not rel.startswith(".github/workflows/"):
            continue
        workflow = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
        for name, job in workflow["jobs"].items():
            runs = "\n".join(step.get("run", "") for step in job.get("steps", []))
            if not MERGE_RE.search(runs):
                continue
            assert _effective_actions_permission(workflow, job) == "write", (
                f"{rel} job {name} needs actions: write"
            )
            assert "trap dispatch_deploy_after_merges EXIT" in runs, (
                f"{rel} job {name} must dispatch once per run"
            )


# --- behaviour: drains dispatch once, after the last merge ---------------------


def _step_script(workflow_rel: str, step_name: str) -> str:
    workflow = yaml.safe_load((ROOT / workflow_rel).read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == step_name:
                return step["run"]
    raise AssertionError(f"{step_name} not found in {workflow_rel}")


def _write_exe(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run_drain(
    tmp_path: Path, workflow_rel: str, *, gate: str = "pass", prs: int = 2, merge_exit: int = 0
) -> list[str]:
    work = tmp_path / "work"
    (work / ".github/scripts").mkdir(parents=True)
    shutil.copy(HELPER, work / ".github/scripts" / HELPER.name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    rows = "".join(f"{100 + i}\\tsha{100 + i}\\tfactory,factory:ready\\n" for i in range(prs))
    _write_exe(
        bin_dir / "gh",
        f"""echo "gh $*" >> {log}
case "$1 $2" in
  "api --paginate"|"pr list") printf '{rows}' ;;
  "pr merge") exit {merge_exit} ;;
  "pr view") echo MERGED ;;
  "workflow run") exit 0 ;;
esac
""",
    )
    _write_exe(
        bin_dir / "python3",
        f"""case "$2" in
  authorized) pr="$4"; echo "{{\\"authorized\\":true,\\"head\\":\\"sha$pr\\"}}" ;;
  gates) echo '{{"decision":"{gate}","reason":"test"}}' ;;
  gate) echo '{{"allow":true,"lane":"normal","reason":"green","main":{{"sha":"{"0" * 40}","state":"green"}}}}' ;;
  after-merge) echo '{{"main":{{"explanation":"main verified pending"}}}}' ;;
  *) exit 0 ;;
esac
""",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "acme/widgets",
        "RUNNER_TEMP": str(tmp_path),
        "GH_TOKEN": "test",
    }
    script = _step_script(workflow_rel, "Drain exact-head ready factory PRs")
    result = subprocess.run(
        ["bash", "-c", script], cwd=work, env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


DRAINS = (".github/workflows/factory-ready-merge-drain.yml",)


def test_drains_dispatch_one_deploy_after_the_last_merge(tmp_path: Path) -> None:
    for index, rel in enumerate(DRAINS):
        calls = _run_drain(tmp_path / str(index), rel)
        merges = [i for i, call in enumerate(calls) if call.startswith("gh pr merge")]
        dispatches = [i for i, call in enumerate(calls) if call.startswith("gh workflow run")]
        # Serialized: one merge per drain run even with two ready PRs; the
        # next merge waits for the resulting main's CI.
        assert len(merges) == 1, (rel, calls)
        assert len(dispatches) == 1, (rel, calls)
        assert dispatches[0] > merges[-1], (rel, calls)
        assert (
            "gh workflow run deploy-production.yml --repo acme/widgets --ref main -f source=factory-merge"
            in calls[dispatches[0]]
        )
        assert "merged 1 PR(s)" in calls[dispatches[0]]


def test_drains_count_prs_merged_while_gh_errored(tmp_path: Path) -> None:
    # gh pr merge fails but the PR is MERGED (the merge landed, or another
    # GITHUB_TOKEN actor merged it): no push event fires, so still dispatch.
    for index, rel in enumerate(DRAINS):
        calls = _run_drain(tmp_path / f"errored{index}", rel, merge_exit=1)
        dispatches = [call for call in calls if call.startswith("gh workflow run")]
        assert len(dispatches) == 1, (rel, calls)
        assert "-f source=factory-merge" in dispatches[0]
        assert "merged 1 PR(s)" in dispatches[0]


def test_drains_without_a_merge_do_not_dispatch(tmp_path: Path) -> None:
    for index, rel in enumerate(DRAINS):
        calls = _run_drain(tmp_path / f"retry{index}", rel, gate="retry")
        assert not [call for call in calls if call.startswith("gh pr merge")]
        assert not [call for call in calls if call.startswith("gh workflow run")], (rel, calls)
        empty = _run_drain(tmp_path / f"empty{index}", rel, prs=0)
        assert not [call for call in empty if call.startswith("gh workflow run")], (rel, empty)


# --- helper and deploy workflow -------------------------------------------------


def _run_helper(
    tmp_path: Path,
    gh_exit: int,
    extra_env: dict[str, str] | None = None,
    *,
    run_list: str = "",
    run_list_exit: int = 0,
    succeed_on_attempt: int = 0,
) -> tuple[int, str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    log = tmp_path / "gh.log"
    count = tmp_path / "dispatches"
    _write_exe(
        bin_dir / "gh",
        f"""echo "gh $*" >> {log}
if [[ "$1 $2" == "run list" ]]; then
  printf '%s' '{run_list}'
  exit {run_list_exit}
fi
n=$(( $(cat {count} 2>/dev/null || echo 0) + 1 )); echo "$n" > {count}
if (( {succeed_on_attempt} > 0 )); then (( n >= {succeed_on_attempt} )) && exit 0 || exit 1; fi
exit {gh_exit}
""",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FACTORY_DEPLOY_DISPATCH_RETRY_SLEEP": "0",
        **(extra_env or {}),
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'set -Eeuo pipefail; source {HELPER}; {DISPATCH_CALL} "test merge"; echo rc=$?',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.returncode, result.stdout + result.stderr, log.read_text() if log.exists() else ""


DISPATCH_LINE = (
    "gh workflow run deploy-production.yml --repo acme/widgets --ref main "
    "-f source=factory-merge -f reason=test merge"
)


def _dispatch_calls(calls: str) -> list[str]:
    return [line for line in calls.splitlines() if line.startswith("gh workflow run")]


def test_helper_dispatches_deploy_on_main_and_never_fails_the_caller(tmp_path: Path) -> None:
    _, out, calls = _run_helper(tmp_path / "ok", 0)
    assert "rc=0" in out
    assert _dispatch_calls(calls) == [DISPATCH_LINE]
    _, out, calls = _run_helper(tmp_path / "fail", 1)
    assert "rc=0" in out and "::warning::Unable to dispatch Deploy Production" in out
    _, out, calls = _run_helper(tmp_path / "off", 0, {"FACTORY_DEPLOY_DISPATCH": "off"})
    assert "rc=0" in out and calls == ""


def test_helper_retries_failed_dispatch_three_times(tmp_path: Path) -> None:
    _, out, calls = _run_helper(tmp_path / "fail", 1)
    assert _dispatch_calls(calls) == [DISPATCH_LINE] * 3
    assert "(3 attempts)" in out
    _, out, calls = _run_helper(tmp_path / "second", 1, succeed_on_attempt=2)
    assert _dispatch_calls(calls) == [DISPATCH_LINE] * 2
    assert "rc=0" in out and "attempt 2/3" in out and "::warning::" not in out
    helper = HELPER.read_text(encoding="utf-8")
    assert "while (( attempt <= 3 )); do" in helper
    assert "sleep $((attempt * backoff))" in helper


def test_helper_does_not_supersede_a_queued_manual_redeploy(tmp_path: Path) -> None:
    # GitHub keeps only the newest pending run in the concurrency group, so a
    # factory-merge dispatch would cancel a queued manual force-redeploy.
    _, out, calls = _run_helper(tmp_path / "queued", 0, run_list="1")
    assert "rc=0" in out and "manual Deploy Production run is already queued" in out
    assert _dispatch_calls(calls) == []
    assert "gh run list --repo acme/widgets --workflow deploy-production.yml" in calls
    assert "--event workflow_dispatch" in calls
    assert "Deploy Production [source=manual]" in calls
    # No manual run queued, or the lookup failed: dispatch as before.
    _, _, calls = _run_helper(tmp_path / "none", 0, run_list="0")
    assert _dispatch_calls(calls) == [DISPATCH_LINE]
    _, _, calls = _run_helper(tmp_path / "lookup-failed", 0, run_list_exit=1)
    assert _dispatch_calls(calls) == [DISPATCH_LINE]


def _pending_manual_count(tmp_path: Path, runs: list[dict[str, str]]) -> str:
    """Run the helper's real ``gh run list --jq`` filter over fixture runs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    fixture = tmp_path / "runs.json"
    fixture.write_text(json.dumps(runs), encoding="utf-8")
    # Fake gh: hand the fixture to the real jq with the helper's own --jq expression.
    _write_exe(
        bin_dir / "gh",
        f"""expr=''
while (( $# )); do
  if [[ "$1" == "--jq" ]]; then expr="$2"; shift; fi
  shift
done
[[ -n "$expr" ]] || exit 2
jq -r "$expr" {fixture}
""",
    )
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"set -Eeuo pipefail; source {HELPER}; factory_pending_manual_deploy_count acme/widgets",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_pending_manual_deploy_filter_matches_only_waiting_manual_runs(tmp_path: Path) -> None:
    assert shutil.which("jq"), "jq is required (installed in Dockerfile.ci)"
    manual = "Deploy Production [source=manual] force redeploy"
    waiting_manual = [
        {"status": status, "displayTitle": manual}
        for status in ("queued", "pending", "waiting", "requested")
    ]
    not_waiting = [
        # Manual runs that already started or finished no longer hold the slot.
        {"status": "in_progress", "displayTitle": manual},
        {"status": "completed", "displayTitle": manual},
        # Queued runs that are not manual dispatches.
        {
            "status": "queued",
            "displayTitle": "Deploy Production [source=factory-merge] merged 2 PR(s)",
        },
        {"status": "pending", "displayTitle": "Deploy Production"},
        {"status": "queued", "displayTitle": "Deploy Production [source=manualish] x"},
    ]
    assert _pending_manual_count(tmp_path / "mixed", waiting_manual + not_waiting) == "4"
    assert _pending_manual_count(tmp_path / "none", not_waiting) == "0"
    assert _pending_manual_count(tmp_path / "empty", []) == "0"


