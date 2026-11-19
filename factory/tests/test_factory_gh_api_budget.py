"""Shared installation REST budget: metering and wakeup fan-out guards."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
METER = ROOT / ".github/scripts/factory-gh-meter.sh"
WORKFLOWS = ROOT / ".github/workflows"
spec = importlib.util.spec_from_file_location("factory_gh_usage", ROOT / ".github/scripts/factory_gh_usage.py")
assert spec and spec.loader
USAGE = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = USAGE
spec.loader.exec_module(USAGE)


def _meter(tmp_path: Path, *args: str) -> str:
    log = tmp_path / "calls.log"
    env = {**os.environ, "FACTORY_GH_CALL_LOG": str(log), "FACTORY_REAL_GH": "/bin/true"}
    subprocess.run(["bash", str(METER), *args], check=True, env=env)
    return log.read_text().splitlines()[-1].split("\t", 1)[1]


def test_meter_normalizes_endpoints_and_methods(tmp_path: Path) -> None:
    assert _meter(tmp_path, "api", "repos/o/r/issues/5299/comments?per_page=100",
                  "--paginate", "--jq", ".[]") == "api GET repos/R/issues/N/comments paginate"
    assert _meter(tmp_path, "api", "repos/o/r/commits/" + "a" * 40 + "/check-runs") \
        == "api GET repos/R/commits/SHA/check-runs"
    # gh api defaults to POST when fields are supplied without -X.
    assert _meter(tmp_path, "api", "repos/o/r/issues/1/comments", "-f", "body=secret") \
        == "api POST repos/R/issues/N/comments"
    assert _meter(tmp_path, "api", "-X", "PUT", "repos/o/r/issues/1/labels") \
        == "api PUT repos/R/issues/N/labels"
    assert _meter(tmp_path, "pr", "view", "12", "--json", "x") == "gh pr view"
    assert "secret" not in (tmp_path / "calls.log").read_text()


def test_usage_summary_counts_writes_and_pagination() -> None:
    usage = USAGE.summarize([
        "1\tapi GET repos/R/pulls/N", "2\tapi GET repos/R/pulls/N",
        "3\tapi PUT repos/R/issues/N/labels", "4\tapi GET repos/R/issues/N/comments paginate",
    ])
    assert usage["total"] == 4 and usage["writes"] == 1 and usage["paginated"] == 1
    assert usage["top"][0] == ("api GET repos/R/pulls/N", 2)


def _step(workflow: str, name: str) -> str:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    for job in data["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == name:
                return step["run"]
    raise AssertionError(name)


def _wake(tmp_path: Path, labels: list[str], pending: int) -> subprocess.CompletedProcess[str]:
    sha = "b" * 40
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": {"head_sha": sha, "pull_requests": [{"number": 5299}]}}))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    pr = {"state": "open", "draft": False, "base": {"ref": "main"}, "head": {"sha": sha},
          "labels": [{"name": name} for name in labels]}
    (bindir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {tmp_path / "gh.log"}\n'
        'case "$*" in\n'
        f"  *check-runs*) echo {pending} ;;\n"
        f"  'api repos/o/r/pulls/5299') echo '{json.dumps(pr)}' ;;\n"
        "  'workflow run'*) ;;\n"
        "esac\n"
    )
    (bindir / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "o/r"}
    script = _step("factory-check-projection.yml", "Wake exact-head PR on check/review completion")
    return subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, check=False)


def _dispatched(tmp_path: Path) -> bool:
    log = tmp_path / "gh.log"
    return log.exists() and "workflow run fixed-model-factory-dispatch.yml" in log.read_text()


def test_check_wakeup_skips_pr_leased_by_active_worker(tmp_path: Path) -> None:
    """Run 44091809870: checkpoint pushes on a leased PR fanned out ~12 dispatches."""
    result = _wake(tmp_path, ["factory", "factory:1127900638100", "factory:changes-requested"], 0)
    assert result.returncode == 0, result.stderr
    assert "leased by an active worker" in result.stdout
    assert not _dispatched(tmp_path)


def test_check_wakeup_waits_for_the_last_running_check(tmp_path: Path) -> None:
    result = _wake(tmp_path, ["factory", "factory:unowned", "factory:review"], 3)
    assert result.returncode == 0, result.stderr
    assert "still has 3 running check(s)" in result.stdout
    assert not _dispatched(tmp_path)


def test_check_wakeup_dispatches_unowned_pr_once_checks_finish(tmp_path: Path) -> None:
    result = _wake(tmp_path, ["factory", "factory:unowned", "factory:review"], 0)
    assert result.returncode == 0, result.stderr
    assert _dispatched(tmp_path)


def test_pr_review_wakeup_skips_leased_prs() -> None:
    script = _step("factory-pr-review-wakeup.yml", "Validate current PR and wake canonical dispatcher")
    assert 'test("^factory:([0-9]+|local)$")' in script
    assert script.index("leased by an active worker") < script.index("gh workflow run")


def test_dispatcher_and_worker_report_gh_usage() -> None:
    dispatch = (WORKFLOWS / "fixed-model-factory-dispatch.yml").read_text()
    assert "FACTORY_GH_CALL_LOG=$RUNNER_TEMP/factory-gh-calls.log" in dispatch
    assert "factory_gh_usage.py" in dispatch
    run = (WORKFLOWS / "free-model-factory-run.yml").read_text()
    assert 'cp .github/scripts/factory-gh-meter.sh "$meter_dir/gh"' in run
    assert 'python3 -I "$meter_dir/factory_gh_usage.py"' in run


def _route(tmp_path: Path, *args: str, expires_in: int = 600) -> str:
    """Run the meter as gh with a fake real gh that reports which token it got."""
    import time

    fake = tmp_path / "real-gh"
    fake.write_text('#!/usr/bin/env bash\necho "${GH_TOKEN:-none}"\n')
    fake.chmod(0o755)
    env = {**os.environ, "FACTORY_REAL_GH": str(fake), "GH_TOKEN": "workflow-token",
           "FACTORY_READ_TOKEN": "reader-token",
           "FACTORY_READ_TOKEN_EXPIRES": str(int(time.time()) + expires_in)}
    env.pop("FACTORY_GH_CALL_LOG", None)
    result = subprocess.run(["bash", str(METER), *args], env=env, text=True,
                            capture_output=True, check=True)
    return result.stdout.strip()


def test_pure_reads_use_the_read_only_app_budget(tmp_path: Path) -> None:
    assert _route(tmp_path, "api", "repos/o/r/pulls/1") == "reader-token"
    assert _route(tmp_path, "api", "repos/o/r/issues/1/comments", "--paginate") == "reader-token"
    assert _route(tmp_path, "pr", "view", "1", "--json", "labels") == "reader-token"
    assert _route(tmp_path, "run", "list") == "reader-token"


def test_writes_and_graphql_keep_the_workflow_token(tmp_path: Path) -> None:
    """Writes stay on GITHUB_TOKEN so they never trigger extra workflows."""
    assert _route(tmp_path, "api", "-X", "PUT", "repos/o/r/issues/1/labels") == "workflow-token"
    assert _route(tmp_path, "api", "repos/o/r/issues/1/comments", "-f", "body=x") == "workflow-token"
    assert _route(tmp_path, "api", "--method=DELETE", "repos/o/r/git/refs/heads/x") == "workflow-token"
    assert _route(tmp_path, "api", "graphql", "-f", "query=x") == "workflow-token"
    assert _route(tmp_path, "workflow", "run", "x.yml") == "workflow-token"
    assert _route(tmp_path, "pr", "merge", "1") == "workflow-token"
    assert _route(tmp_path, "issue", "comment", "1") == "workflow-token"


def test_expired_read_token_falls_back_to_the_workflow_token(tmp_path: Path) -> None:
    assert _route(tmp_path, "api", "repos/o/r/pulls/1", expires_in=-5) == "workflow-token"


def test_read_token_is_optional_and_verified_before_use() -> None:
    for name in ("fixed-model-factory-dispatch.yml", "free-model-factory-run.yml"):
        data = yaml.safe_load((WORKFLOWS / name).read_text())
        steps = [s for job in data["jobs"].values() for s in job.get("steps", [])]
        resolve = next(s for s in steps if s.get("name") == "Resolve existing read-only App client ID")
        assert resolve["if"] == "${{ env.FACTORY_READER_APP_ID != '' }}"
        assert resolve["continue-on-error"] is True
        assert "factory_app_client_id.py" in resolve["run"]
        mint = next(s for s in steps if s.get("name") == "Mint optional read-only factory App token")
        assert mint["continue-on-error"] is True
        assert mint["if"] == "${{ steps.read_app_identity.outputs.client_id != '' }}"
        assert mint["with"]["client-id"] == "${{ steps.read_app_identity.outputs.client_id }}"
        assert "app-id" not in mint["with"]
        assert all(v == "read" for k, v in mint["with"].items() if k.startswith("permission-"))
        verify = next(s for s in steps if s.get("name") == "Verify and enable read-only factory App token")
        assert verify["run"].index("gh api") < verify["run"].index("FACTORY_READ_TOKEN=")


SHIM = ROOT / ".github/scripts/factory-gh-rest-shim.sh"


def _shim(tmp_path: Path, *args: str, ttl: str = "45") -> str:
    calls = tmp_path / "live-calls"
    fake = tmp_path / "real-gh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {calls}\n'
        'echo "live:$(wc -l < ' + str(calls) + ')"\n'
    )
    fake.chmod(0o755)
    env = {**os.environ, "FACTORY_REAL_GH": str(fake), "GITHUB_WORKSPACE": str(ROOT),
           "FACTORY_DISCOVERY_DIR": str(tmp_path / "discovery"),
           "FACTORY_GH_READ_CACHE_TTL": ttl, "GITHUB_REPOSITORY": "o/r"}
    for name in ("FACTORY_GH_CALL_LOG", "FACTORY_READ_TOKEN"):
        env.pop(name, None)
    return subprocess.run(["bash", str(SHIM), *args], env=env, text=True,
                          capture_output=True, check=True).stdout.strip()


def _live(tmp_path: Path) -> int:
    calls = tmp_path / "live-calls"
    return len(calls.read_text().splitlines()) if calls.exists() else 0


def test_dispatcher_reuses_repeated_status_reads_within_one_job(tmp_path: Path) -> None:
    checks = ("pr", "checks", "5", "--repo", "o/r", "--required", "--json", "state")
    runs = ("api", "--paginate", "--slurp",
            "repos/o/r/actions/workflows/free-model-factory-entry.yml/runs?status=queued&per_page=100")
    assert _shim(tmp_path, *checks) == "live:1"
    assert _shim(tmp_path, *checks) == "live:1"
    assert _shim(tmp_path, *runs) == "live:2"
    assert _shim(tmp_path, *runs) == "live:2"
    assert _live(tmp_path) == 2


def test_any_write_drops_the_read_cache(tmp_path: Path) -> None:
    checks = ("pr", "checks", "5", "--repo", "o/r", "--required", "--json", "state")
    _shim(tmp_path, *checks)
    _shim(tmp_path, "api", "-X", "PUT", "repos/o/r/issues/5/labels")
    assert _shim(tmp_path, *checks) == "live:3"
    _shim(tmp_path, "workflow", "run", "free-model-factory-entry.yml")
    assert _shim(tmp_path, *checks) == "live:5"


def test_ownership_reads_and_expired_entries_stay_live(tmp_path: Path) -> None:
    assert _shim(tmp_path, "api", "repos/o/r/issues/5") == "live:1"
    assert _shim(tmp_path, "api", "repos/o/r/issues/5") == "live:2"
    checks = ("pr", "checks", "5", "--repo", "o/r", "--required", "--json", "state")
    _shim(tmp_path, *checks, ttl="0")
    assert _shim(tmp_path, *checks, ttl="0") == "live:4"


def _dispatch_batch(tmp_path: Path, assignments: list[str]) -> tuple[int, str]:
    """Run the real roster dispatch step with stubbed controller and gh."""
    script = _step("fixed-model-factory-dispatch.yml", "Resolve and dispatch fixed workers")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "assign.log"
    queue = tmp_path / "queue"
    queue.write_text("\n".join(assignments) + "\n")
    (bindir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *factory_worker_slots.py\\ offer*) echo \'["1","2","3","4","5","6","7","8"]\' ;;\n'
        '  *"assign --worker"*) echo "$*" >> ' + str(log) + "\n"
        '    head -1 ' + str(queue) + '; sed -i 1d ' + str(queue) + ' ;;\n'
        '  *" capacity"*) echo \'{"remaining": 12}\' ;;\n'
        '  *" reconcile"*|*" release"*) ;;\n'
        '  *" filter"*|*" rank"*) cat ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    (bindir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in *rate_limit*) echo 5000 ;; *) ;; esac\n'
    )
    for tool in ("python3", "gh"):
        (bindir / tool).chmod(0o755)
    output = tmp_path / "out"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "EVENT_NAME": "workflow_dispatch",
           "DISPATCH_MODE": "roster", "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(tmp_path)}
    env.pop("OPENROUTER_JEV_API_KEY", None)
    result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    calls = len(log.read_text().splitlines()) if log.exists() else 0
    return calls, result.stdout


def test_idle_roster_tick_stops_after_three_empty_assignments(tmp_path: Path) -> None:
    calls, out = _dispatch_batch(tmp_path, ['{"kind":"none"}'] * 8)
    assert calls == 3
    assert "3 consecutive workers found no executable work" in out


def test_an_assignment_resets_the_idle_streak(tmp_path: Path) -> None:
    none = '{"kind":"none"}'
    work = '{"kind":"issue","number":1}'
    calls, _ = _dispatch_batch(tmp_path, [none, none, work, none, none, none, none, none])
    assert calls == 6


def test_usage_report_separates_cache_hits_from_github_requests() -> None:
    usage = USAGE.summarize(["1\tgh pr checks", "2\tgh pr checks", "2\tcache-hit"])
    assert usage["total"] == 2 and usage["cache_hits"] == 1 and usage["github"] == 1


def test_quota_sampler_is_read_only_and_on_demand() -> None:
    data = yaml.safe_load((WORKFLOWS / "factory-api-quota-sampler.yml").read_text())
    assert set(data[True]) == {"workflow_dispatch"}
    assert data["permissions"] == {"actions": "read", "contents": "read"}
