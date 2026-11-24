"""Installed adopter workflows run the pinned Rotisserie factory runtime."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

FACTORY = Path(__file__).resolve().parents[1]
REUSABLE = FACTORY.parent / ".github" / "workflows"
SPEC = importlib.util.spec_from_file_location(
    "rotisserie_install", FACTORY / "install" / "install.py"
)
assert SPEC and SPEC.loader
install = importlib.util.module_from_spec(SPEC)
sys.modules["rotisserie_install"] = install
SPEC.loader.exec_module(install)

REF = "a" * 40
OVERLAY_PATHS = (FACTORY / "install" / "overlay-paths.txt").read_text().split()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True)) or {}


CALLERS = sorted((FACTORY / "install" / "callers").glob("*.yml"))
LEVEL = {"none": 0, "read": 1, "write": 2}
# A called workflow may request cache writes these triggers would not grant.
LOW_TRUST = {
    "workflow_run",
    "issues",
    "issue_comment",
    "pull_request_target",
    "pull_request_review",
    "pull_request_review_comment",
}


def _job_permissions(reusable: dict) -> list[dict]:
    blocks = [reusable.get("permissions") or {}]
    for job in reusable["jobs"].values():
        blocks.append(job.get("permissions") or {})
        local = str(job.get("uses", ""))
        if local.startswith("./.github/workflows/"):
            blocks.extend(_job_permissions(_load(REUSABLE / Path(local).name)))
    return blocks


# NEON_API_KEY stays declared in store workflows until adopters re-run the
# installer, so old callers fail closed; new callers never pass it there.
TRANSITIONAL = "Kept until every adopter re-runs the installer"


def _secrets_used(name: str) -> set[str]:
    text = (REUSABLE / name).read_text(encoding="utf-8")
    used = set(re.findall(r"secrets\.([A-Z0-9_]+)", text)) - {"GITHUB_TOKEN"}
    if TRANSITIONAL in text:
        used.discard("NEON_API_KEY")
    for local in re.findall(r"uses: \./\.github/workflows/(\S+)", text):
        used |= _secrets_used(local)
    return used


@pytest.mark.parametrize("caller", CALLERS, ids=lambda p: p.name)
def test_caller_is_thin_and_matches_its_reusable_workflow(caller: Path) -> None:
    workflow = _load(caller)
    assert set(workflow) - {"name", "run-name", True, "permissions", "concurrency", "jobs"} == set()
    assert list(workflow["jobs"]) == ["rotisserie"]
    job = workflow["jobs"]["rotisserie"]
    assert set(job) <= {"cache-mode", "uses", "with", "secrets"}
    if set(_triggers(workflow)) & LOW_TRUST:
        assert job["cache-mode"] == "read"
    assert job["uses"] == f"JoshCLWren/rotisserie/.github/workflows/{caller.name}@main"
    reusable = _load(REUSABLE / caller.name)
    assert list(_triggers(reusable)) == ["workflow_call"]
    call = _triggers(reusable)["workflow_call"] or {}
    dispatch = _triggers(workflow).get("workflow_dispatch") or {}
    assert (
        set(job.get("with") or {})
        == set(call.get("inputs") or {})
        == set(dispatch.get("inputs") or {})
    )
    for name, value in (job.get("with") or {}).items():
        kind = dispatch["inputs"][name].get("type", "string")
        assert call["inputs"][name]["type"] == ("boolean" if kind == "boolean" else "string")
        suffix = " == true }}" if kind == "boolean" else " }}"
        assert value == f"${{{{ inputs.{name}{suffix}"
    declared = set(call.get("secrets") or {})
    if TRANSITIONAL in (REUSABLE / caller.name).read_text(encoding="utf-8"):
        declared.discard("NEON_API_KEY")
    assert declared == _secrets_used(caller.name)
    assert job.get("secrets", {}) == {name: f"${{{{ secrets.{name} }}}}" for name in declared}
    granted = workflow["permissions"]
    for block in _job_permissions(reusable):
        for scope, level in block.items():
            assert LEVEL[granted.get(scope, "none")] >= LEVEL[level], (scope, level)
    assert "concurrency" not in reusable and "run-name" not in reusable


def test_every_reusable_factory_workflow_has_a_caller_or_a_local_caller() -> None:
    reusable = {path.name for path in REUSABLE.glob("*.yml")} - {"ci.yml", "canary-merge.yml"}
    called_locally = {
        Path(match).name
        for path in REUSABLE.glob("*.yml")
        for match in re.findall(r"uses: \./(\.github/workflows/\S+)", path.read_text())
    }
    assert reusable == {path.name for path in CALLERS} | called_locally


@pytest.mark.parametrize(
    "reusable",
    sorted(set(REUSABLE.glob("*.yml")) - {REUSABLE / "ci.yml", REUSABLE / "canary-merge.yml"}),
    ids=lambda p: p.name,
)
def test_every_adopter_checkout_is_followed_by_the_matching_runtime(reusable: Path) -> None:
    for job, spec in _load(reusable)["jobs"].items():
        steps = spec.get("steps") or []
        python = next(
            (i for i, s in enumerate(steps) if "setup-python" in str(s.get("uses", ""))), None
        )
        for index, step in enumerate(steps):
            run = str(step.get("run", ""))
            if python is None or index < python:
                assert not re.search(r"\bpython3?\b", run), job
                assert not str(step.get("uses", "")).startswith("./"), job
            options = step.get("with") or {}
            if not str(step.get("uses", "")).startswith("actions/checkout") or options.get(
                "repository"
            ) not in (None, "${{ github.repository }}"):
                continue
            runtime, overlay = steps[index + 1], steps[index + 2]
            assert runtime["with"]["repository"] == "${{ job.workflow_repository }}", job
            assert runtime["with"]["ref"] == "${{ job.workflow_sha }}", job
            assert runtime["with"]["persist-credentials"] is False, job
            assert "secrets." not in str(runtime), job
            assert "overlay.sh" in overlay["run"], job
            assert runtime.get("if") == step.get("if"), job
            assert overlay.get("if") == step.get("if"), job
        if python is not None:
            assert str(steps[python]["with"]["python-version"]) == "3.14", job


def test_render_points_the_caller_at_the_source_and_ref() -> None:
    caller = (FACTORY / "install" / "callers" / "factory-ready-merge-drain.yml").read_text()
    rendered = install.render(caller, source="example/rotisserie", ref=REF)
    assert f"uses: example/rotisserie/.github/workflows/factory-ready-merge-drain.yml@{REF}" in (
        rendered
    )
    assert (
        rendered.replace(
            f"example/rotisserie/.github/workflows/factory-ready-merge-drain.yml@{REF}",
            "JoshCLWren/rotisserie/.github/workflows/factory-ready-merge-drain.yml@main",
        )
        == caller
    )


def test_ref_tracks_a_branch_or_pins_an_exact_commit() -> None:
    caller = CALLERS[0].read_text()
    for ref in ("main", "a" * 40):
        assert f"@{ref}" in install.render(caller, source="example/rotisserie", ref=ref)
    for bad in ("", "-main", "main branch", "main..evil", "ref;rm"):
        with pytest.raises(install.InstallError):
            install.render(caller, source="example/rotisserie", ref=bad)
    with pytest.raises(install.InstallError):
        install.render(caller, source="bad source", ref="main")


def test_installer_tracks_main_by_default() -> None:
    assert install.DEFAULT_REF == "main"


def test_overlay_never_ships_adopter_state_or_adopter_docs() -> None:
    forbidden = (
        ".github/factory-quota-pauses.json",
        ".github/factory-rotisserie-control.json",
        ".github/factory-source-budgets.json",
        ".github/factory-worker-github-apps.json",
        ".github/factory-worker-github-apps.md",
        ".github/NON_FACTORY_ACTIONS_PAUSED",
        "FACTORY_GATE_READY",
        "docs/ISSUE_EXECUTION_PROTOCOL.md",
        "docs/PRODUCT_ACCEPTANCE_PROTOCOL.md",
        ".rotisserie/config.toml",
        ".github/actions/fork-ci-toolchain",
    )
    assert not set(forbidden) & {path.rstrip("/") for path in OVERLAY_PATHS}
    assert all(not path.startswith(".github/workflows/") for path in OVERLAY_PATHS)
    assert all((FACTORY / path.rstrip("/")).exists() for path in OVERLAY_PATHS)


def _adopter(tmp_path: Path) -> Path:
    repo = tmp_path / "adopter"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".rotisserie").mkdir()
    (repo / ".rotisserie" / "config.toml").write_text('[repository]\nowner = "o"\nname = "r"\n')
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    runtime = repo / ".rotisserie" / "runtime" / "factory"
    shutil.copytree(
        FACTORY, runtime, ignore=shutil.ignore_patterns(".venv", "__pycache__", "tests")
    )
    return repo


def test_overlay_is_invisible_to_git_and_exports_runtime_paths(tmp_path: Path) -> None:
    repo = _adopter(tmp_path)
    env_file = tmp_path / "github_env"
    subprocess.run(
        ["bash", ".rotisserie/runtime/factory/install/overlay.sh", "."],
        cwd=repo,
        check=True,
        env={"PATH": "/usr/bin:/bin", "GITHUB_ENV": str(env_file)},
    )
    assert (repo / ".github" / "scripts" / "factory_checkpoint_handoff.py").is_file()
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    _git(repo, "add", "-A")
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    exported = env_file.read_text()
    assert f"ROTISSERIE_FACTORY_ROOT={repo / '.rotisserie/runtime/factory'}" in exported
    assert f"ROTISSERIE_CONFIG={repo / '.rotisserie/config.toml'}" in exported


def test_overlay_refuses_an_adopter_that_still_tracks_factory_code(tmp_path: Path) -> None:
    repo = _adopter(tmp_path)
    tracked = repo / ".github" / "scripts" / "factory_checkpoint_handoff.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("adopter copy\n")
    _git(repo, "add", ".github")
    _git(
        repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "old factory"
    )
    result = subprocess.run(
        ["bash", ".rotisserie/runtime/factory/install/overlay.sh", "."],
        cwd=repo,
        text=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 1
    assert ".github/scripts/factory_checkpoint_handoff.py" in result.stderr
    assert tracked.read_text() == "adopter copy\n"


def test_install_writes_callers_under_their_own_names(tmp_path: Path) -> None:
    repo = tmp_path / "adopter"
    repo.mkdir()
    _git(repo, "init", "-q")
    written, removed = install.install(
        repo, source="example/rotisserie", ref=REF, names=["factory-ready-merge-drain.yml"]
    )
    assert written == [repo / ".github" / "workflows" / "factory-ready-merge-drain.yml"]
    assert removed == []
    with pytest.raises(install.InstallError):
        install.install(repo, source="example/rotisserie", ref=REF, names=["missing.yml"])


def test_full_install_writes_every_caller_and_removes_retired_files(tmp_path: Path) -> None:
    repo = tmp_path / "adopter"
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    _git(repo, "init", "-q")
    (workflows / "free-model-factory-run.yml").write_text("old\n")
    (workflows / "deploy-production.yml").write_text("adopter\n")
    written, removed = install.install(repo, source="example/rotisserie", ref=REF)
    assert {path.name for path in written} == {path.name for path in CALLERS}
    assert removed == [workflows / "free-model-factory-run.yml"]
    assert (workflows / "deploy-production.yml").read_text() == "adopter\n"


def test_every_factory_script_is_overlaid() -> None:
    listed = {path.rstrip("/") for path in OVERLAY_PATHS}
    scripts = [
        path
        for path in (FACTORY / ".github" / "scripts").iterdir()
        if path.is_file() and not path.name.startswith("test_") and ".test." not in path.name
    ]
    missing = sorted(
        f".github/scripts/{path.name}"
        for path in scripts
        if f".github/scripts/{path.name}" not in listed
    )
    assert missing == []


ADOPTER_OWNED_DOCS = (
    "docs/AUTONOMOUS_FACTORY_POLICY.md",
    "docs/CHATGPT_FACTORY_PROMPT.md",
    "docs/FACTORY_GITHUB_VISIBILITY.md",
    "docs/RUST_FACTORY_ENDPOINT_PATTERN.md",
    "prompts/agent-next-task.md",
    "scripts/check-autonomous-factory-policy.py",
)


def test_overlay_never_ships_an_adopters_product_policy() -> None:
    paths = (FACTORY / "install" / "overlay-paths.txt").read_text(encoding="utf-8").split()
    for doc in ADOPTER_OWNED_DOCS:
        assert doc not in paths
        assert not (FACTORY / doc).exists()


def test_worker_prompt_names_no_adopter_files() -> None:
    worker = (FACTORY / ".github/scripts/free-model-factory-worker-primitives.sh").read_text()
    assert "Read AGENTS.md first and follow its instructions" in worker
    for doc in ADOPTER_OWNED_DOCS:
        assert doc not in worker
