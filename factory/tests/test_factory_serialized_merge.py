"""Serialized merges: each resulting main is verified before the next merge."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from tests.workflow_files import adopter_view

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "factory_main_health.py"
WORKFLOWS = REPO_ROOT.parent / ".github" / "workflows"
SHA = "a" * 40
OTHER = "b" * 40


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("factory_main_health_serial", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


def run(
    run_id: int,
    *,
    workflow: str = "ci.yml",
    event: str = "push",
    head_sha: str = SHA,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, object]:
    return {
        "id": run_id,
        "path": f".github/workflows/{workflow}",
        "name": workflow,
        "event": event,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
    }


def page(*runs: dict[str, object]) -> dict[str, object]:
    return {"workflow_runs": list(runs)}


def test_no_run_is_unverified_never_green() -> None:
    health = MODULE.classify(page(), SHA)
    assert health["state"] == "unverified"
    assert health["missing"] == ["ci.yml"]
    assert "unverified" in health["explanation"]


def test_completed_success_is_green() -> None:
    assert MODULE.classify(page(run(1)), SHA)["state"] == "green"


def test_dispatch_run_counts_like_push() -> None:
    health = MODULE.classify(page(run(1, event="workflow_dispatch")), SHA)
    assert health["state"] == "green"


def test_in_progress_is_pending() -> None:
    health = MODULE.classify(page(run(1, status="in_progress", conclusion=None)), SHA)
    assert health["state"] == "pending"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure"])
def test_failed_or_cancelled_is_red(conclusion: str) -> None:
    assert MODULE.classify(page(run(1, conclusion=conclusion)), SHA)["state"] == "red"


def test_other_shas_pull_requests_and_other_workflows_are_ignored() -> None:
    health = MODULE.classify(
        page(
            run(1, head_sha=OTHER, conclusion="failure"),
            run(2, event="pull_request", conclusion="failure"),
            run(3, workflow="factory-ready-merge-drain.yml", conclusion="failure"),
        ),
        SHA,
    )
    assert health["state"] == "unverified"


def test_newest_run_per_workflow_wins() -> None:
    flaky_then_fixed = page(run(1, conclusion="failure"), run(2, event="workflow_dispatch"))
    assert MODULE.classify(flaky_then_fixed, SHA)["state"] == "green"
    fixed_then_broken = page(run(2, conclusion="failure"), run(1))
    assert MODULE.classify(fixed_then_broken, SHA)["state"] == "red"


def test_every_configured_workflow_must_pass() -> None:
    workflows = ("ci.yml", "rust.yml")
    payload = [page(run(1)), page()]
    health = MODULE.classify(payload, SHA, workflows)
    assert health["state"] == "unverified"
    assert health["missing"] == ["rust.yml"]
    red_beats_pending = [page(run(1, status="queued", conclusion=None)), page(run(2, workflow="rust.yml", conclusion="failure"))]
    assert MODULE.classify(red_beats_pending, SHA, workflows)["state"] == "red"


def test_configured_workflows_parse_and_validate() -> None:
    assert MODULE.configured_workflows("") == ("ci.yml",)
    assert MODULE.configured_workflows(" ci.yml, rust.yml ,ci.yml") == ("ci.yml", "rust.yml")
    with pytest.raises(ValueError):
        MODULE.configured_workflows("../evil.yml")


def health(state: str) -> dict[str, object]:
    return {"sha": SHA, "state": state}


def test_merge_decision_lanes() -> None:
    decide = MODULE.merge_decision
    assert decide(health("green"), [])["allow"] is True
    assert decide(health("green"), ["main-breakage"])["lane"] == "main-repair"
    assert decide(health("red"), [])["allow"] is False
    repair = decide(health("red"), ["main-breakage"])
    assert repair["allow"] is True and repair["lane"] == "main-repair"
    for state in ("pending", "unverified"):
        for labels in ([], ["main-breakage"]):
            assert decide(health(state), labels)["allow"] is False


def test_claim_staleness() -> None:
    now = 1_800_000_000.0
    fresh = "2027-01-15T08:00:00+00:00"
    stamp = MODULE.datetime.fromisoformat(fresh).timestamp()
    assert MODULE.claim_is_stale(fresh, stamp + 60) is False
    assert MODULE.claim_is_stale(fresh, stamp + MODULE.CLAIM_TTL_SECONDS + 1) is True
    assert MODULE.claim_is_stale("not a date", now) is True


class FakeGitHub:
    """Minimal in-memory GitHub for the gh-backed gate."""

    def __init__(self, runs: list[dict[str, object]], labels: list[str]) -> None:
        self.runs = runs
        self.labels = labels
        self.refs: dict[str, str] = {}
        self.tags: dict[str, dict[str, object]] = {}
        self.dispatched: list[str] = []
        self.main = SHA

    def gh(self, args: list[str], *, payload: object | None = None) -> str:
        if args[0] == "workflow":
            self.dispatched.append(args[2])
            return ""
        method, path = args[2], args[3]
        repo = "repos/o/r/"
        assert path.startswith(repo)
        path = path[len(repo):]
        if path == "commits/main":
            return json.dumps({"sha": self.main})
        if path.startswith("actions/workflows/"):
            name = path.split("/")[2]
            return json.dumps(page(*[r for r in self.runs if str(r["path"]).endswith(name)]))
        if path.startswith("issues/"):
            return json.dumps({"labels": [{"name": n} for n in self.labels]})
        if path == "git/tags" and method == "POST":
            assert isinstance(payload, dict)
            sha = f"{len(self.tags) + 1:040d}"
            self.tags[sha] = {"tagger": payload["tagger"], "message": payload["message"]}
            return json.dumps({"sha": sha})
        if path == "git/refs" and method == "POST":
            assert isinstance(payload, dict)
            ref = str(payload["ref"]).removeprefix("refs/")
            if ref in self.refs:
                raise MODULE.GitHubError("HTTP 422: Reference already exists")
            self.refs[ref] = str(payload["sha"])
            return json.dumps({"ref": ref})
        if path.startswith("git/ref/"):
            ref = path.removeprefix("git/ref/")
            if ref not in self.refs:
                raise MODULE.GitHubError("HTTP 404: Not Found")
            return json.dumps({"object": {"type": "tag", "sha": self.refs[ref]}})
        if path.startswith("git/tags/"):
            return json.dumps(self.tags[path.removeprefix("git/tags/")])
        if path.startswith("git/refs/") and method == "DELETE":
            ref = path.removeprefix("git/refs/")
            if ref not in self.refs:
                raise MODULE.GitHubError("HTTP 422: Reference does not exist")
            del self.refs[ref]
            return ""
        raise AssertionError(f"unexpected gh call {args}")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    github = FakeGitHub([run(1)], [])
    monkeypatch.setattr(MODULE, "_gh", github.gh)
    monkeypatch.delenv("FACTORY_MAIN_CI_WORKFLOWS", raising=False)
    return github


def test_gate_claims_green_main_exclusively(fake: FakeGitHub) -> None:
    first = MODULE.gate("o/r", 10)
    assert first["allow"] is True and first["claimed"] is True
    second = MODULE.gate("o/r", 11)
    assert second["allow"] is False
    assert "already claimed" in str(second["reason"])


def test_after_merge_releases_and_dispatches_new_main(fake: FakeGitHub) -> None:
    assert MODULE.gate("o/r", 10)["claimed"] is True
    fake.main = OTHER  # the merge moved main to an unverified commit
    after = MODULE.after_merge("o/r", SHA)
    assert after["released"] is True
    main = after["main"]
    assert isinstance(main, dict) and main["state"] == "unverified"
    assert fake.dispatched == ["ci.yml"]
    # The next merge waits for the new main's verdict.
    assert MODULE.gate("o/r", 11)["allow"] is False


def test_unverified_main_dispatches_ci_and_refuses_merge(fake: FakeGitHub) -> None:
    fake.runs = []
    result = MODULE.gate("o/r", 10)
    assert result["allow"] is False and result["claimed"] is False
    assert result["dispatched"] == ["ci.yml"]
    assert fake.refs == {}


def test_red_main_admits_only_main_repair(fake: FakeGitHub) -> None:
    fake.runs = [run(1, conclusion="failure")]
    assert MODULE.gate("o/r", 10)["allow"] is False
    fake.labels = ["main-breakage"]
    repair = MODULE.gate("o/r", 10)
    assert repair["allow"] is True and repair["lane"] == "main-repair"


def test_stale_claim_from_a_crashed_actor_is_reclaimed(fake: FakeGitHub) -> None:
    assert MODULE.acquire_claim("o/r", SHA, 10, now=1_000.0) is True
    assert MODULE.acquire_claim("o/r", SHA, 11, now=1_000.0 + 60) is False
    assert MODULE.acquire_claim("o/r", SHA, 11, now=1_000.0 + MODULE.CLAIM_TTL_SECONDS + 1) is True


# --- workflow contracts --------------------------------------------------------


def test_only_the_serialized_drain_merges_ready_prs() -> None:
    drain = adopter_view("factory-ready-merge-drain.yml")
    dispatcher = adopter_view("fixed-model-factory-dispatch.yml")
    assert "gh pr merge" not in dispatcher
    assert "ensure-verification" in dispatcher
    assert "gh workflow run factory-ready-merge-drain.yml" in dispatcher
    assert "group: factory-ready-merge-drain" in drain
    assert "cancel-in-progress: false" in drain
    merge = drain.index('gh pr merge "$pr"')
    assert drain.index('"$main_health" gate --repo') < merge
    assert drain.index("ensure-verification") < merge
    assert "after-merge --repo" in drain
    # One snapshot per run: the gate reuses it and the listing's labels.
    assert '--labels "$labels" --health-file "$RUNNER_TEMP/factory-main-health.json"' in drain
    assert drain.count("ensure-verification") == 1
    assert "FACTORY_MAIN_CI_WORKFLOWS: ${{ vars.FACTORY_MAIN_CI_WORKFLOWS }}" in drain


def test_canonical_worker_route_uses_the_same_serialized_gate() -> None:
    route = (REPO_ROOT / ".github" / "scripts" / "factory-canonical-merge-route.sh").read_text(
        encoding="utf-8"
    )
    merge = route.index("gh pr merge")
    assert route.index('"$main_health" gate --repo') < merge
    assert "after-merge --repo" in route
    for name in (
        "free-model-factory-worker.sh",
        "omniroute-factory-worker.sh",
        "free-model-factory-worker-primitives.sh",
        "nvidia-factory-worker.sh",
    ):
        text = (REPO_ROOT / ".github" / "scripts" / name).read_text(encoding="utf-8")
        assert 'cp .github/scripts/factory_main_health.py "$trusted_dir/factory_main_health.py"' in text


def test_cli_prints_classification(tmp_path: Path) -> None:
    payload = tmp_path / "runs.json"
    payload.write_text(json.dumps(page({**run(1, conclusion="failure"), "name": "CI"})))
    legacy = subprocess.run(
        ["python3", str(SCRIPT), "--sha", SHA],
        stdin=payload.open(),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ},
    )
    assert json.loads(legacy.stdout)["failing"] == 1
