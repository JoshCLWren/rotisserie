"""Trusted pre-push fast checks for factory worker diffs.

Regression shapes: a duplicate `const productionOrigin` in an .mjs file
(#3879, worker 20) and a removed `useQueueThreads` import that was still
called (#3895, worker 11) both reached PRs because nothing ran before push.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"
HELPER = SCRIPTS / "factory-fast-checks.sh"
PRIMITIVES = SCRIPTS / "free-model-factory-worker-primitives.sh"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=repo, check=True
    )


def repo_with(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return repo


def stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def run_checks(
    repo: Path, *, env: dict[str, str] | None = None, prelude: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f"set -Eeuo pipefail; source {HELPER!s}; {prelude}\n"
            "status=0; factory_run_fast_checks HEAD fast.log || status=$?; "
            "cat fast.log; exit $status",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def test_plan_routes_each_touched_file_type_to_its_checks(tmp_path: Path) -> None:
    repo = repo_with(
        tmp_path,
        {
            "scripts/build/generate-route-html.mjs": "export {};\n",
            ".github/scripts/factory-visibility.cjs": "module.exports = {};\n",
            "frontend/src/pages/Queue.tsx": "export {};\n",
            "frontend/src/api.ts": "export {};\n",
            "app/service.py": "x = 1\n",
            "tests/test_service.py": "def test_x():\n    pass\n",
            "tests/test_other.py": "def test_y():\n    pass\n",
            "docs/readme.md": "x\n",
        },
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {HELPER!s}; factory_fast_check_paths HEAD | factory_fast_check_plan",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    plan = set(result.stdout.splitlines())
    assert "node\tscripts/build/generate-route-html.mjs" in plan
    assert "node\t.github/scripts/factory-visibility.cjs" in plan
    assert "frontend\tfrontend/src/pages/Queue.tsx" in plan
    assert "frontend\tfrontend/src/api.ts" in plan
    assert "python\tapp/service.py" in plan
    # A touched module pulls in its own test file; touched tests run themselves.
    assert "pytest\ttests/test_service.py" in plan
    assert "pytest\ttests/test_other.py" in plan
    assert not any("readme" in line for line in plan)


def test_duplicate_const_in_mjs_blocks_push(tmp_path: Path) -> None:
    repo = repo_with(
        tmp_path,
        {
            "scripts/build/generate-route-html.mjs": (
                "const productionOrigin = 'a';\nconst productionOrigin = 'b';\n"
            )
        },
    )
    result = run_checks(repo)
    assert result.returncode == 1
    assert "Identifier 'productionOrigin' has already been declared" in result.stdout
    assert "FAST CHECKS FAILED: node --check scripts/build/generate-route-html.mjs" in result.stdout


def test_valid_node_files_pass_and_untouched_types_need_no_toolchain(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"a.cjs": "module.exports = 1;\n", "docs/x.md": "y\n"})
    result = run_checks(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAST CHECKS PASSED" in result.stdout


def test_removed_import_still_called_fails_frontend_typecheck(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"frontend/src/pages/QueuePage.tsx": "useQueueThreads();\n"})
    bin_dir = tmp_path / "bin"
    stub(
        bin_dir,
        "pnpm",
        'echo "pnpm $*" >> ../pnpm.log\n'
        'case "$*" in\n'
        "  *typecheck*) echo \"src/pages/QueuePage.tsx(1,1): error TS2304: Cannot find name 'useQueueThreads'.\"; exit 2 ;;\n"
        "esac\nexit 0",
    )
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 1
    assert "Cannot find name 'useQueueThreads'" in result.stdout
    assert "frontend typecheck" in result.stdout
    calls = (tmp_path / "pnpm.log").read_text()
    assert "install --frozen-lockfile" in calls
    assert "--filter frontend run typecheck" in calls
    assert "--dir frontend exec eslint -- src/pages/QueuePage.tsx" in calls


def python_stub(
    bin_dir: Path, pytest_output: str, pytest_status: int, ruff_status: int = 0
) -> None:
    stub(
        bin_dir,
        "uv",
        'echo "uv $*" >> ../uv.log\n'
        'case "$*" in\n'
        f"  *pytest*) printf '%s\\n' {pytest_output!r}; exit {pytest_status} ;;\n"
        f"  *ruff*) echo 'app/x.py:1:1: F821 Undefined name `helper`'; exit {ruff_status} ;;\n"
        "esac\nexit 0",
    )


def test_python_checks_run_ci_parity_lint_types_and_targeted_tests(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"app/service.py": "x = 1\n", "tests/test_service.py": "x = 1\n"})
    bin_dir = tmp_path / "bin"
    python_stub(bin_dir, "1 passed", 0)
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 0, result.stdout
    calls = (tmp_path / "uv.log").read_text()
    assert "sync --frozen --all-extras" in calls
    assert "run --frozen ruff check ." in calls
    assert "run --frozen ty check --error-on-warning" in calls
    assert "pytest -q -p no:cacheprovider --no-cov -x tests/test_service.py" in calls



def test_openapi_paths_select_schema_freshness_check(tmp_path: Path) -> None:
    """Contract sources and generated artifacts must select the contract gate."""
    paths = {
        "native/contracts/v2-runtime.openapi.json": "{}\n",
        "scripts/export_openapi_schema.mjs": "export {};\n",
        "frontend/src/generated/openapi.json": "{}\n",
        "frontend/src/generated/openapi.ts": "export {};\n",
        "frontend/src/generated/nativeV2Client.ts": "export {};\n",
    }
    repo = repo_with(tmp_path, paths)
    result = subprocess.run(
        ["bash", "-c", f"source {HELPER!s}; factory_fast_check_paths HEAD | factory_fast_check_plan"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    plan = set(result.stdout.splitlines())
    for path in paths:
        assert f"openapi\t{path}" in plan


def test_stale_openapi_blocks_factory_push(tmp_path: Path) -> None:
    """A stale generated schema fails closed while the change is still local."""
    repo = repo_with(tmp_path, {"native/contracts/v2-runtime.openapi.json": "{}\n"})
    bin_dir = tmp_path / "bin"
    stub(
        bin_dir,
        "node",
        'echo "node $*" >> ../node.log\n'
        'case "$*" in\n'
        '  *export_openapi_schema.mjs*--check*) echo "Generated OpenAPI schema is stale"; exit 1 ;;\n'
        'esac\nexit 0',
    )
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAST CHECKS FAILED: OpenAPI schema freshness" in result.stdout
    assert "Generated OpenAPI schema is stale" in result.stdout
    assert "scripts/export_openapi_schema.mjs --check" in (tmp_path / "node.log").read_text()


def test_current_openapi_passes_factory_gate(tmp_path: Path) -> None:
    """A current schema does not block the push."""
    repo = repo_with(tmp_path, {"frontend/src/generated/openapi.json": "{}\n"})
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "node", 'echo "node $*" >> ../node.log\nexit 0')
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert "FAST CHECKS FAILED" not in result.stdout, result.stdout
    assert "export_openapi_schema.mjs --check" in (tmp_path / "node.log").read_text()


def test_missing_openapi_toolchain_blocks_generated_schema_change(tmp_path: Path) -> None:
    """Missing node fails closed rather than silently pushing a stale schema."""
    repo = repo_with(tmp_path, {"frontend/src/generated/openapi.json": "{}\n"})
    result = run_checks(repo, prelude="command() { return 1; }")
    assert result.returncode == 1
    assert "OpenAPI check toolchain (node)" in result.stdout


def test_manifest_only_change_checks_frozen_pnpm_lockfile(tmp_path: Path) -> None:
    """A package-only change fails before push if its lockfile is stale."""
    repo = repo_with(tmp_path, {"package.json": '{"devDependencies": {"x": "2"}}\n'})
    bin_dir = tmp_path / "bin"
    stub(
        bin_dir,
        "pnpm",
        'echo "pnpm $*" >> ../pnpm.log\n'
        'case "$*" in\n'
        '  *install*--frozen-lockfile*) echo "ERR_PNPM_OUTDATED_LOCKFILE"; exit 1 ;;\n'
        'esac\nexit 0',
    )
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ERR_PNPM_OUTDATED_LOCKFILE" in result.stdout
    assert "frontend toolchain setup (pnpm install)" in result.stdout
    calls = (tmp_path / "pnpm.log").read_text()
    assert "install --frozen-lockfile" in calls
    assert "typecheck" not in calls


def test_package_and_lockfile_changes_select_dependency_gate(tmp_path: Path) -> None:
    """Root and frontend dependency files must select frozen installs."""
    repo = repo_with(
        tmp_path,
        {
            "package.json": "{}\n",
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            "frontend/package.json": "{}\n",
            "pnpm-workspace.yaml": "packages: []\n",
        },
    )
    result = subprocess.run(
        ["bash", "-c", f"source {HELPER!s}; factory_fast_check_paths HEAD | factory_fast_check_plan"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    for path in (
        "package.json",
        "pnpm-lock.yaml",
        "frontend/package.json",
        "pnpm-workspace.yaml",
    ):
        assert f"dependencies\t{path}" in result.stdout.splitlines()


def test_ruff_failure_blocks_push(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"app/service.py": "helper()\n"})
    bin_dir = tmp_path / "bin"
    python_stub(bin_dir, "", 0, ruff_status=1)
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 1
    assert "F821 Undefined name" in result.stdout


def test_targeted_test_failure_blocks_but_database_only_errors_do_not(tmp_path: Path) -> None:
    for output, status, blocked in (
        ("E   NameError: name 'helper' is not defined", 1, True),
        (
            "E   ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 5432)",
            1,
            False,
        ),
        ("timed out", 124, True),
    ):
        case = tmp_path / f"case{status}{int(blocked)}"
        case.mkdir()
        repo = repo_with(case, {"tests/test_service.py": "x = 1\n"})
        bin_dir = case / "bin"
        python_stub(bin_dir, output, status)
        result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        assert (result.returncode == 1) is blocked, (output, result.stdout)


def test_toolchain_setup_failure_fails_closed(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"app/service.py": "x = 1\n"})
    result = run_checks(repo, prelude="_factory_fast_check_ensure_uv() { return 1; }")
    assert result.returncode == 1
    assert "python toolchain setup (uv)" in result.stdout


def test_incident_switch_disables_the_gate(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"x.mjs": "const a = 1; const a = 2;\n"})
    assert run_checks(repo, env={"FACTORY_FAST_CHECKS": "off"}).returncode == 0


def extract(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    return source[start : source.index("\n}\n", start) + 3]


def test_persist_never_commits_or_pushes_when_fast_checks_fail(tmp_path: Path) -> None:
    source = PRIMITIVES.read_text()
    for name, call in (
        ("_persist_pr_changes_untraced", "_persist_pr_changes_untraced 42 branch"),
        ("_persist_issue_pr_untraced", "_persist_issue_pr_untraced 7 branch"),
    ):
        body = extract(source, name)
        assert body.index("reject_out_of_scope_diff") < body.index("factory_run_fast_checks")
        assert body.index("factory_run_fast_checks") < body.index("git add -A")
        script = f"""
set -Eeuo pipefail
EXPECTED_HEAD=base WORKER=11 FACTORY_PUSH_BLOCKED_REASON=''
log() {{ :; }}
reject_out_of_scope_diff() {{ return 0; }}
factory_run_fast_checks() {{ return 1; }}
factory_report_fast_check_failure() {{ echo "report:$*" >&2; }}
factory_push_credential() {{ echo "PUSH-CREDENTIAL" >&2; return 1; }}
git() {{ echo "git:$*" >&2; [[ "$1" == status ]] && echo changed; return 0; }}
{body}
status=0; {call} || status=$?
echo "status=$status reason=$FACTORY_PUSH_BLOCKED_REASON"
"""
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)
        assert "status=1 reason=fast-checks-failed" in result.stdout, result.stderr
        assert "git:add" not in result.stderr and "git:commit" not in result.stderr
        assert "PUSH-CREDENTIAL" not in result.stderr
        assert "git:reset" not in result.stderr
        assert "report:" in result.stderr


def test_fast_checks_are_staged_from_trusted_main_before_checkout() -> None:
    source = PRIMITIVES.read_text()
    assert source.index("factory-fast-checks.sh") < source.index("checkout_target() {")
    # The live worker imports primitives only above the legacy-loop sentinel.
    assert source.index("factory-fast-checks.sh") < source.index("\nensure_owner_label\n")


def test_deletions_still_select_whole_project_checks(tmp_path: Path) -> None:
    # Deleting a still-imported module must not produce an empty plan
    # (Greptile finding on #3899): whole-project checks still run, while
    # file-level commands only receive files that exist.
    repo = repo_with(
        tmp_path,
        {"frontend/src/hooks/useQueueThreads.ts": "export {};\n", "app/helpers.py": "x = 1\n"},
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "files")
    (repo / "frontend/src/hooks/useQueueThreads.ts").unlink()
    (repo / "app/helpers.py").unlink()
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "pnpm", 'echo "pnpm $*" >> ../pnpm.log\nexit 0')
    python_stub(bin_dir, "", 0)
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 0, result.stdout
    pnpm = (tmp_path / "pnpm.log").read_text()
    assert "--filter frontend run typecheck" in pnpm
    assert "eslint" not in pnpm
    uv = (tmp_path / "uv.log").read_text()
    assert "run --frozen ruff check ." in uv and "ty check --error-on-warning" in uv
    assert "pytest" not in uv


def test_renames_check_both_the_removed_and_the_added_path(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"scripts/a.mjs": "export {};\n"})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "files")
    git(repo, "mv", "scripts/a.mjs", "scripts/b.mjs")
    result = subprocess.run(
        ["bash", "-c", f"source {HELPER!s}; factory_fast_check_paths HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    assert set(result.stdout.split()) == {"scripts/a.mjs", "scripts/b.mjs"}


def run_hook_toolchain(repo: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f"set -Eeuo pipefail; source {HELPER!s}; factory_ensure_commit_hook_toolchain",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )


def test_hook_toolchain_is_installed_for_non_python_diffs(tmp_path: Path) -> None:
    # Greptile finding on #3914: the trusted pre-commit hook calls
    # scripts/lint.sh, which requires .venv/ruff before it inspects staged file
    # types, so a docs- or frontend-only repair must still install them or the
    # commit fails with "ruff not found" and produces no repair bundle.
    repo = repo_with(tmp_path, {"docs/readme.md": "x\n"})
    bin_dir = tmp_path / "bin"
    stub(
        bin_dir,
        "uv",
        'echo "uv $*" >> ../uv.log\n'
        'if [[ "$1" == sync ]]; then mkdir -p .venv/bin && : > .venv/bin/ruff; fi\n'
        "exit 0",
    )
    result = run_hook_toolchain(repo, bin_dir)
    assert result.returncode == 0, result.stderr
    assert (repo / ".venv/bin/ruff").exists()
    assert "sync --frozen --all-extras" in (tmp_path / "uv.log").read_text()


def test_hook_toolchain_is_a_noop_when_the_venv_is_present(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"docs/readme.md": "x\n"})
    ruff = repo / ".venv/bin/ruff"
    ruff.parent.mkdir(parents=True)
    ruff.write_text("#!/bin/sh\n")
    ruff.chmod(0o755)
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "uv", 'echo "uv $*" >> ../uv.log\nexit 0')
    result = run_hook_toolchain(repo, bin_dir)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "uv.log").exists()


def test_fork_runner_installs_hook_toolchain_before_committing() -> None:
    source = (SCRIPTS / "factory-fork-execute.sh").read_text()
    assert source.index("factory_ensure_commit_hook_toolchain") < source.index(
        "install-git-hooks.sh"
    )
    assert source.index("install-git-hooks.sh") < source.index("git commit")


def rust_fixture(repo: Path) -> None:
    """Install the real preflight runner in an isolated synthetic git checkout."""
    source = ROOT / "tests/fixtures/adopter-hooks/check-rust-factory-prepush.sh"
    dest = repo / "scripts/check-rust-factory-prepush.sh"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source.read_text())


def rust_stubs(tmp_path: Path, *, fail_clippy: bool = False) -> Path:
    """Stub the external toolchain without needing a compiler or database."""
    bin_dir = tmp_path / "bin"
    stub(bin_dir, "rustup", "exit 0")
    stub(
        bin_dir,
        "cargo",
        'echo "cargo $*" >> ../cargo.log\n'
        + (
            'case "$*" in *clippy*) echo "error[E0308]: mismatched types"; exit 101 ;; esac\n'
            if fail_clippy
            else ""
        )
        + "exit 0",
    )
    stub(bin_dir, "uv", 'echo "uv $*" >> ../uv.log\nexit 0')
    return bin_dir


def test_rust_sources_manifests_and_native_rewrites_select_preflight(tmp_path: Path) -> None:
    """Rust, Cargo and native ingress changes must not be invisible to the gate."""
    repo = repo_with(
        tmp_path,
        {
            "native/roll-service/src/queue.rs": "pub fn demo() {}\n",
            "api/native_roll.rs": "fn main() {}\n",
            "native/roll-service/Cargo.toml": "[package]\nname = 'demo'\n",
            "Cargo.lock": "lock\n",
            "rust-toolchain.toml": "[toolchain]\nchannel = 'stable'\n",
            "vercel.json": "{}\n",
        },
    )
    result = subprocess.run(
        ["bash", "-c", f"source {HELPER!s}; factory_fast_check_paths HEAD | factory_fast_check_plan"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    plan = set(result.stdout.splitlines())
    assert "rust\tnative/roll-service/src/queue.rs" in plan
    assert "rust\tapi/native_roll.rs" in plan
    assert "rust\tnative/roll-service/Cargo.toml" in plan
    assert "rust\tCargo.lock" in plan
    assert "rust\trust-toolchain.toml" in plan
    assert "ingress\tvercel.json" in plan
    assert "ingress\tapi/native_roll.rs" in plan


def test_rust_compile_error_blocks_push_and_runs_focused_ingress_guards(tmp_path: Path) -> None:
    """A bad Rust first head is rejected before GitHub Actions."""
    repo = repo_with(tmp_path, {"native/roll-service/src/queue.rs": "bad rust\n"})
    rust_fixture(repo)
    bin_dir = rust_stubs(tmp_path, fail_clippy=True)
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "error[E0308]" in result.stdout
    assert "Rust format/Clippy/native build" in result.stdout
    assert "cargo +stable fmt" in (tmp_path / "cargo.log").read_text()
    assert "cargo +stable clippy" in (tmp_path / "cargo.log").read_text()
    assert "test_native_roll_live_ingress.py" in (tmp_path / "uv.log").read_text()
    assert "test_native_roll_write_test_guard.py" in (tmp_path / "uv.log").read_text()
    assert "test_vercel_deployment.py" in (tmp_path / "uv.log").read_text()


def test_valid_rust_checks_both_crates_and_deletion_still_checks(tmp_path: Path) -> None:
    """The gate checks the shared workspace, including deletes."""
    repo = repo_with(tmp_path, {"api/native_roll.rs": "fn main() {}\n"})
    rust_fixture(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Rust file")
    (repo / "api/native_roll.rs").unlink()
    bin_dir = rust_stubs(tmp_path)
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 0, result.stdout + result.stderr
    cargo = (tmp_path / "cargo.log").read_text()
    assert "cargo +stable fmt --all -- --check" in cargo
    assert "cargo +stable clippy --workspace --locked --all-targets -- -D warnings" in cargo
    assert "cargo +stable check --workspace --locked --all-targets" in cargo


def test_vercel_rewrite_only_change_runs_routing_tests_not_cargo(tmp_path: Path) -> None:
    """Vercel changes get the relevant tests without a pointless Rust build."""
    repo = repo_with(tmp_path, {"vercel.json": '{"rewrites": []}\n'})
    bin_dir = rust_stubs(tmp_path)
    result = run_checks(repo, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "cargo.log").exists()
    assert "test_vercel_deployment.py" in (tmp_path / "uv.log").read_text()
