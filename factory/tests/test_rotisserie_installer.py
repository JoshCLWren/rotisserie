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


def _steps(text: str) -> list[tuple[str, list[dict]]]:
    workflow = yaml.safe_load(text)
    return [(name, job.get("steps") or []) for name, job in (workflow.get("jobs") or {}).items()]


@pytest.mark.parametrize(
    "template", sorted((FACTORY / ".github" / "workflows").glob("*.yml")), ids=lambda p: p.name
)
def test_every_adopter_checkout_is_followed_by_the_pinned_runtime(template: Path) -> None:
    rendered = install.render(
        template.read_text(encoding="utf-8"), source="example/rotisserie", ref=REF
    )
    for job, steps in _steps(rendered):
        for index, step in enumerate(steps):
            uses = str(step.get("uses", ""))
            options = step.get("with") or {}
            if not uses.startswith("actions/checkout") or options.get("repository") not in (
                None,
                "${{ github.repository }}",
            ):
                continue
            runtime, overlay = steps[index + 1], steps[index + 2]
            assert runtime["with"]["repository"] == "example/rotisserie", job
            assert runtime["with"]["ref"] == REF, job
            assert runtime["with"]["persist-credentials"] is False, job
            assert "secrets." not in str(runtime), job
            assert "overlay.sh" in overlay["run"], job
            assert runtime.get("if") == step.get("if"), job
            assert overlay.get("if") == step.get("if"), job


def test_render_preserves_template_text_outside_inserted_steps() -> None:
    template = (FACTORY / ".github" / "workflows" / "factory-ready-merge-drain.yml").read_text(
        encoding="utf-8"
    )
    rendered = install.render(template, source="example/rotisserie", ref=REF)
    kept = [line for line in rendered.splitlines() if install.INSTALLED_MARKER not in line]
    assert all(line in kept for line in template.splitlines())


def test_checkout_into_a_subdirectory_overlays_that_directory() -> None:
    template = (
        "on: workflow_dispatch\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v7\n        with:\n          path: factory-control\n"
        "      - run: echo done\n"
    )
    steps = _steps(install.render(template, source="example/rotisserie", ref=REF))[0][1]
    assert steps[1]["with"]["path"] == "factory-control/.rotisserie/runtime"
    assert steps[2]["run"].endswith("overlay.sh factory-control")
    assert steps[3] == {"run": "echo done"}


def test_other_repository_checkouts_are_not_overlaid() -> None:
    template = (
        "on: workflow_dispatch\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v7\n        with:\n          repository: someone/else\n"
    )
    assert install.render(template, source="example/rotisserie", ref=REF) == template


def test_ref_must_be_an_exact_commit() -> None:
    with pytest.raises(install.InstallError):
        install.render("jobs: {}\n", source="example/rotisserie", ref="main")


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


def test_install_writes_templates_under_their_own_names(tmp_path: Path) -> None:
    repo = tmp_path / "adopter"
    repo.mkdir()
    _git(repo, "init", "-q")
    written = install.install(
        repo, source="example/rotisserie", ref=REF, names=["factory-ready-merge-drain.yml"]
    )
    assert written == [repo / ".github" / "workflows" / "factory-ready-merge-drain.yml"]
    with pytest.raises(install.InstallError):
        install.install(repo, source="example/rotisserie", ref=REF, names=["missing.yml"])


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


def test_templates_that_run_factory_tests_are_not_installed_by_default(tmp_path: Path) -> None:
    runs_tests = sorted(
        path.name
        for path in (FACTORY / ".github" / "workflows").glob("*.yml")
        if re.search(r"\bpytest\b|node --test", path.read_text(encoding="utf-8"))
    )
    assert runs_tests == sorted(install.ROTISSERIE_CI_ONLY)
    repo = tmp_path / "adopter"
    repo.mkdir()
    _git(repo, "init", "-q")
    installed = {path.name for path in install.install(repo, source="example/rotisserie", ref=REF)}
    assert not installed & install.ROTISSERIE_CI_ONLY
    assert "factory-ready-merge-drain.yml" in installed


def test_conditional_checkout_conditions_the_runtime_steps() -> None:
    template = (
        "on: workflow_dispatch\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: Checkout only on failure\n        if: >-\n          env.FAILED == 'true'\n"
        "          && github.event_name == 'workflow_run'\n        uses: actions/checkout@v7\n"
        "      - run: echo done\n"
    )
    steps = _steps(install.render(template, source="example/rotisserie", ref=REF))[0][1]
    condition = "env.FAILED == 'true' && github.event_name == 'workflow_run'"
    assert steps[0]["if"] == condition
    assert steps[1]["if"] == condition and steps[1]["with"]["repository"] == "example/rotisserie"
    assert steps[2]["if"] == condition and "overlay.sh" in steps[2]["run"]
    assert steps[3] == {"run": "echo done"}
