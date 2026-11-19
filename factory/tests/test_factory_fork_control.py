"""Exercise the contributor execution boundary with real Git objects and forged data."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import zipfile

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / ".github/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_control() -> ModuleType:
    """Load the fork controller without a static package import on sys.path."""
    path = SCRIPTS / "factory_fork_control.py"
    spec = importlib.util.spec_from_file_location("factory_fork_control_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control = load_control()


def test_static_review_has_no_repository_credentials_or_tools(tmp_path: Path) -> None:
    """Provider access cannot carry repository tokens, integrations, or broad permissions."""
    original = tmp_path / "original/.config/opencode"
    original.mkdir(parents=True)
    (original / "opencode.json").write_text(json.dumps({
        "providers": {"nvidia": {"name": "selected"}, "other": {"secret": "unrelated"}},
        "plugins": ["poison"], "permissions": [{"action": "*", "effect": "allow"}],
    }))
    env = control._static_environment(tmp_path / "clean", "nvidia", {
        "HOME": str(tmp_path / "original"), "PATH": "/usr/bin",
        "GH_TOKEN": "writer", "PR_REBASE_TOKEN": "writer", "NVIDIA_API_KEY": "provider",
        "OPENROUTER_API_KEY": "other", "OPENCODE_CONFIG": "/poison/config",
        "SSH_AUTH_SOCK": "agent", "ACTIONS_RUNTIME_TOKEN": "artifact-writer",
    }, "nvidia/model")
    assert env["NVIDIA_API_KEY"] == "provider"
    assert not {"GH_TOKEN", "PR_REBASE_TOKEN", "OPENROUTER_API_KEY", "OPENCODE_CONFIG",
                "SSH_AUTH_SOCK", "ACTIONS_RUNTIME_TOKEN"} & env.keys()
    config = json.loads((tmp_path / "clean/.config/opencode/opencode.json").read_text())
    assert config["providers"] == {"nvidia": {"name": "selected"}}
    assert config["permissions"] == [{"action": "*", "resource": "*", "effect": "deny"}]
    assert {row["action"] for row in config["policies"] if row["effect"] == "deny"} == {
        "tool.use", "integration.use",
    }
    assert "plugins" not in config



def test_static_fork_review_uses_dedicated_route_and_only_its_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An NVIDIA coding worker must review via OpenRouter without GitHub credentials."""
    original = tmp_path / "original/.config/opencode"
    original.mkdir(parents=True)
    (original / "opencode.json").write_text(json.dumps({
        "providers": {"openrouter": {"models": {"free": {"modelID": "openrouter/free"}}}}
    }))
    monkeypatch.setenv("HOME", str(tmp_path / "original"))
    monkeypatch.setenv("FACTORY_SOURCE", "openrouter-free")
    monkeypatch.setenv("FACTORY_MODEL", "openrouter/free")
    monkeypatch.setenv("FACTORY_RUNTIME_MODEL", "openrouter/free")
    monkeypatch.setenv("OPENROUTER_API_KEY", "review-only")
    monkeypatch.setenv("NVIDIA_API_KEY", "coding-key")
    monkeypatch.setenv("GH_TOKEN", "writer")
    monkeypatch.setattr(control, "_source", lambda _worker: "nvidia")
    monkeypatch.setattr(
        control, "_context", lambda _pr, _head, path: path.write_text("Reviewed diff")
    )
    monkeypatch.chdir(tmp_path)
    seen: list[dict[str, str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        seen.append(env)
        model_index = args.index("-m")
        assert args[model_index + 1] == "openrouter/free"
        config = json.loads(
            (Path(env["HOME"]) / ".config/opencode/opencode.json").read_text()
        )
        assert list(config["providers"]) == ["openrouter"]
        assert all(item["effect"] == "deny" for item in config["permissions"])
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(control.subprocess, "run", fake_run)
    assert control._review(4923, "a" * 40, "52", "openrouter/free", 30, tmp_path / "review.log") == 1
    assert len(seen) == 1
    assert seen[0]["OPENROUTER_API_KEY"] == "review-only"
    assert "NVIDIA_API_KEY" not in seen[0]
    assert "GH_TOKEN" not in seen[0]

    monkeypatch.setenv("FACTORY_SOURCE", "nvidia")
    with pytest.raises(ValueError, match="inconsistent dedicated"):
        control._review(4923, "a" * 40, "52", "openrouter/free", 30, tmp_path / "invalid.log")


def test_execution_rejects_writer_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail before fetching source or metadata if a privileged token reaches execution."""
    # Simulate the real secretless main workflow so only the credential fence can fire.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", f"{control.REPO}/{control.EXECUTION_PATH}@refs/heads/main")
    monkeypatch.setenv("PR_REBASE_TOKEN", "poison")
    with pytest.raises(ValueError, match="privileged credentials"):
        control._verify_execution(3897, "a" * 40, "46", "b" * 32)


@pytest.mark.parametrize("login,app", [("contributor", "github-actions"), ("github-actions[bot]", "other"), ("github-actions[bot]", None)])
def test_receipt_requires_controller_bot_and_app(login: str, app: str | None) -> None:
    """Copying a receipt marker or bot display name cannot authorize persistence."""
    row = {"user": {"login": login}, "performed_via_github_app": {"slug": app},
           "body": f"<!-- comic-pile-factory-fork-dispatch-v1:pr-3897:head-{'a' * 40}:worker-46:nonce-{'b' * 32} -->"}
    with pytest.raises(ValueError, match="receipt"):
        control._receipt([row], 3897, "46", "b" * 32)
    row["user"] = {"login": "github-actions[bot]"}
    row["performed_via_github_app"] = {"slug": "github-actions"}
    assert control._receipt([row], 3897, "46", "b" * 32) == "a" * 40


@pytest.mark.parametrize("field,value", [
    ("repository", {"full_name": "attacker/comic-pile"}),
    ("head_repository", {"full_name": "attacker/comic-pile"}),
    ("event", "pull_request"), ("head_branch", "poison"),
    ("workflow_id", 999), ("status", "in_progress"),
    ("display_title", "Factory 46 · fake"),
])
def test_run_identity_cannot_be_forged(field: str, value: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use authoritative run identity, not artifact-supplied fields."""
    monkeypatch.setattr(control, "_source", lambda worker: "kilo-auto")
    run = {"repository": {"full_name": control.REPO}, "head_repository": {"full_name": control.REPO},
           "event": "workflow_dispatch", "head_branch": "main", "workflow_id": 7,
           "status": "completed", "display_title": f"Factory 46 · fork repair · PR 3897 · {'b' * 32}"}
    workflow = {"id": 7, "path": control.EXECUTION_PATH}
    assert control._validate_run(run, workflow) == (3897, "46", "b" * 32)
    run[field] = value
    with pytest.raises(ValueError):
        control._validate_run(run, workflow)


@pytest.mark.parametrize("filename", ["../repair.bundle", "/tmp/payload", "hooks/post-checkout", "result.json/evil"])
def test_artifact_paths_are_never_extracted(filename: str) -> None:
    """Traversal and executable extras fail before reading an artifact result."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("result.json", "{}")
        archive.writestr(filename, "poison")
    with pytest.raises(ValueError, match="paths"):
        control._artifact(data.getvalue())


def test_artifact_accepts_only_bounded_proposal_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid ZIP is data; decompression limits also cover compressed bombs."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("result.json", '{"status":"changed"}')
        archive.writestr("repair.bundle", "a" * 2000)
    result, bundle = control._artifact(data.getvalue())
    assert result == {"status": "changed"}
    assert bundle == b"a" * 2000
    monkeypatch.setattr(control, "MAX_ARTIFACT_BYTES", 1000)
    with pytest.raises(ValueError, match="unpacked"):
        control._artifact(data.getvalue())


def test_bare_persistence_checks_real_parent_without_checkout(tmp_path: Path) -> None:
    """Accept one descendant, reject multi-commit history, never run a poisoned hook."""
    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=source, text=True, stderr=subprocess.DEVNULL).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Contributor")
    git("config", "user.email", "contributor@example.com")
    (source / "file").write_text("base")
    git("add", ".")
    git("commit", "-m", "base")
    expected = git("rev-parse", "HEAD")
    (source / "file").write_text("repair")
    poison = source / ".githooks/post-checkout"
    poison.parent.mkdir()
    marker = tmp_path / "executed"
    poison.write_text(f"#!/bin/sh\ntouch {marker}\n")
    poison.chmod(0o755)
    git("add", ".")
    git("commit", "-m", "repair")
    git("branch", "factory-fork-result")
    bundle = tmp_path / "repair.bundle"
    git("bundle", "create", str(bundle), "refs/heads/factory-fork-result", f"^{expected}")
    with control._verified_bundle(bundle.read_bytes(), expected, str(source)) as (bare, commit):
        assert commit == git("rev-parse", "HEAD")
        assert (bare / "HEAD").exists()
        assert not (bare / "file").exists()
    assert not marker.exists()
    (source / "file").write_text("second repair")
    git("commit", "-am", "second")
    git("branch", "-f", "factory-fork-result")
    git("bundle", "create", str(bundle), "refs/heads/factory-fork-result", f"^{expected}")
    with pytest.raises(ValueError, match="one descendant"):
        with control._verified_bundle(bundle.read_bytes(), expected, str(source)):
            pytest.fail("multi-commit proposal accepted")


def test_fork_workflows_separate_execution_and_persistence() -> None:
    """Untrusted source has only read permissions; privileged job never checks it out."""
    root = SCRIPTS.parent / "workflows"
    execution_text = (root / "factory-fork-execution.yml").read_text()
    execution = yaml.safe_load(execution_text)
    assert set(execution["permissions"].values()) == {"read"}
    assert "secrets." not in execution_text
    checkout = execution["jobs"]["repair"]["steps"][0]
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["persist-credentials"] is False
    persistence = yaml.safe_load((root / "factory-fork-persistence.yml").read_text())
    steps = persistence["jobs"]["persist"]["steps"]
    assert steps[0]["with"]["ref"] == "main"
    assert not any("download-artifact" in step.get("uses", "") for step in steps)
    assert "factory_fork_control.py complete" in steps[-1]["run"]


def test_failure_cannot_release_someone_elses_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old execution result cannot steal ownership from a newer worker."""
    monkeypatch.setattr(control, "_read_pr", lambda pr: {"labels": [{"name": "factory:73"}]})
    monkeypatch.setattr(control, "_post", lambda *args: pytest.fail("posted stale failure"))
    control._release_failure(3897, "46", "a" * 40, "fork-repair-execution-failed", "b" * 32)


def test_stale_run_cannot_release_a_newer_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rereconciled old run must not release the lease of a newer same-worker dispatch."""
    monkeypatch.setattr(
        control,
        "_read_pr",
        lambda pr: {"labels": [{"name": "factory:46"}]},
    )
    monkeypatch.setattr(control, "_comments", lambda pr: [
        {"user": {"login": "github-actions[bot]"}, "performed_via_github_app": {"slug": "github-actions"},
         "body": f"<!-- comic-pile-factory-fork-dispatch-v1:pr-3897:head-{'a' * 40}:worker-46:nonce-{'b' * 32} -->"},
        {"user": {"login": "github-actions[bot]"}, "performed_via_github_app": {"slug": "github-actions"},
         "body": f"<!-- comic-pile-factory-fork-dispatch-v1:pr-3897:head-{'a' * 40}:worker-46:nonce-{'c' * 32} -->"},
    ])
    monkeypatch.setattr(control, "_post", lambda *args: pytest.fail("stale run released a newer claim"))
    monkeypatch.setattr(control, "_set_state", lambda *args: pytest.fail("stale run rewrote newer state"))
    control._release_failure(3897, "46", "a" * 40, "fork-repair-execution-failed", "b" * 32)


def test_handled_failure_is_marked_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed or no-change run posts a completion marker so it is not retried."""
    posted: list[str] = []
    monkeypatch.setattr(control, "_post", lambda pr, body: posted.append(body))
    control._mark_completed(3897, "b" * 32, "a" * 40, "no-change")
    assert posted == [f"<!-- comic-pile-factory-fork-completed-v1:nonce-{'b' * 32}:head-{'a' * 40}:result-no-change -->"]


def test_completed_failure_run_is_skipped_on_resweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completion marker for the run's nonce short-circuits reprocessing."""
    nonce = "b" * 32
    monkeypatch.setattr(control, "_validate_run", lambda run, workflow: (3897, "46", nonce))
    monkeypatch.setattr(control, "_read_pr", lambda pr: {"labels": [{"name": "factory:46"}]})
    monkeypatch.setattr(control, "_receipt", lambda comments, pr, worker, nonce_arg: "a" * 40)
    monkeypatch.setattr(control, "_comments", lambda pr: [
        {"user": {"login": "github-actions[bot]"}, "performed_via_github_app": {"slug": "github-actions"},
         "body": f"<!-- comic-pile-factory-fork-completed-v1:nonce-{nonce}:head-{'a' * 40}:result-no-change -->"},
    ])
    monkeypatch.setattr(control, "_gh", lambda args, payload=None: {"id": 1, "path": control.EXECUTION_PATH, "event": "workflow_dispatch", "head_branch": "main"})
    assert control._persist(123) == {"status": "already-completed", "pr": 3897}


def test_fork_review_never_runs_the_tool_enabled_recovery_pass() -> None:
    """Recovery starts a tool-enabled agent, so forks must never reach it."""
    worker = (SCRIPTS / "free-model-factory-worker.sh").read_text(encoding="utf-8")
    recovery_condition = worker.split('if [[ -z "$verdict" ]]', 1)[1].split("; then", 1)[0]
    assert "FACTORY_FORK_STATIC_REVIEW" in recovery_condition


def test_binary_file_does_not_block_the_review_snapshot() -> None:
    """A PNG or other binary asset is described, not decoded as UTF-8."""
    binary = _payload(b"\x89PNG\r\n\x1a\n\x00\xff")
    section = control._source_section("assets/logo.png", "a" * 40, binary)
    assert section.startswith("Binary file assets/logo.png at " + "a" * 40)
    assert "content omitted" in section
    text = _payload(b"print('ok')\n")
    assert control._source_section("app.py", "a" * 40, text) == f"Source file app.py at {'a' * 40}:\nprint('ok')\n"


def _payload(raw: bytes) -> dict[str, object]:
    return {
        "encoding": "base64",
        "size": str(len(raw)),
        "content": base64.b64encode(raw).decode(),
    }


def test_static_fork_review_gate_targets_the_exact_head() -> None:
    """The Greptile gate must use the reviewed fork head, not the trusted main HEAD."""
    worker = (SCRIPTS / "free-model-factory-worker.sh").read_text(encoding="utf-8")
    gate = worker.split("greptile_outcome_gate() {", 1)[1].split("\n}\n", 1)[0]
    assert "FACTORY_FORK_STATIC_REVIEW" in gate
    assert 'head="$EXPECTED_HEAD"' in gate


def test_credentialed_routing_never_checks_out_a_fork() -> None:
    """The worker dispatches or snapshots forks; checkout refuses fork heads."""
    routing = (SCRIPTS / "factory-fork-worker.sh").read_text(encoding="utf-8")
    assert "dispatch --pr" in routing
    assert "FACTORY_FORK_STATIC_REVIEW=1" in routing
    primitives = (SCRIPTS / "free-model-factory-worker-primitives.sh").read_text(encoding="utf-8")
    assert "refusing fork checkout in a credentialed worker" in primitives
