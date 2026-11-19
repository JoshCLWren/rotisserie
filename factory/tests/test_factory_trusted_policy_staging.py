"""Exercise the trusted worker policy bundle outside the source checkout."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_staged_worker_policy_imports_only_pinned_eligibility(tmp_path: Path) -> None:
    """The worker must not depend on /scripts or an untrusted PR worktree."""
    worker = (ROOT / ".github/scripts/free-model-factory-worker.sh").read_text()
    assert 'cp scripts/factory_eligibility.py "$trusted_dir/factory_eligibility.py"' in worker

    staged = tmp_path / "trusted"
    staged.mkdir()
    for name in ("factory_work_policy.py", "factory_review_policy.py"):
        shutil.copy2(ROOT / ".github/scripts" / name, staged / name)
    shutil.copy2(ROOT / "scripts/factory_eligibility.py", staged / "factory_eligibility.py")

    check = """
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
import factory_work_policy
canonical = pathlib.Path(sys.argv[1], 'factory_eligibility.py').resolve()
assert pathlib.Path(factory_work_policy.eligibility.__file__).resolve() == canonical
assert factory_work_policy.eligibility.is_manual_only('<!-- factory-execution:manual-only -->')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", check, str(staged)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_stage_does_not_copy_policy_from_pr_checkout() -> None:
    worker = (ROOT / ".github/scripts/free-model-factory-worker.sh").read_text()
    staging = worker.split("stage_trusted_review_controller() {", 1)[1].split("\n}", 1)[0]
    assert staging.index('cp scripts/factory_eligibility.py') < staging.index('TRUSTED_REVIEW_CONTROLLER=')
    assert '[[ -z "${TRUSTED_REVIEW_CONTROLLER:-}" ]] || return 0' in staging


def test_dependency_merge_gate_modules_are_staged_from_trusted_main() -> None:
    """Both worker stages pin the dependency parser and closure module."""
    for name in ("free-model-factory-worker.sh", "free-model-factory-worker-primitives.sh"):
        content = (ROOT / ".github/scripts" / name).read_text()
        staging = content.split("stage_trusted_review_controller() {", 1)[1].split("\n}", 1)[0]
        for dep in (
            ".github/scripts/factory_post_merge_closure.py",
            ".github/scripts/factory_implementation_delivery.py",
            "scripts/factory_eligibility.py",
        ):
            assert f"cp {dep} " in staging, (name, dep)


def test_staged_review_controller_starts_without_source_checkout(tmp_path: Path) -> None:
    """The staged review controller must load its implementation-delivery helper.

    Regression for #4104 (Greptile PR #4050, 6 open P1s): before #4050 the
    trusted staging copied the controller without
    ``factory_implementation_delivery.py``, so the staged controller died at
    import with ``ModuleNotFoundError`` and no factory command could start.
    Copy exactly the bundle both worker wrappers stage and prove the staged
    controller starts in isolation from the source checkout.
    """
    staged = tmp_path / "trusted"
    staged.mkdir()
    bundle = (
        ".github/scripts/factory-review-controller.py",
        ".github/scripts/factory_review_policy.py",
        ".github/scripts/factory_issue_acceptance.py",
        ".github/scripts/factory_implementation_delivery.py",
        ".github/scripts/factory_post_merge_closure.py",
        ".github/scripts/factory_worker_slots.py",
        ".github/scripts/factory_provider_registry.py",
        ".github/scripts/factory_work_policy.py",
        ".github/scripts/factory_fork_control.py",
        "scripts/factory_eligibility.py",
    )
    for relative in bundle:
        shutil.copy2(ROOT / relative, staged / Path(relative).name)

    result = subprocess.run(
        [sys.executable, "-I", str(staged / "factory-review-controller.py"), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "review" in result.stdout
    assert "gates" in result.stdout
