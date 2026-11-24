"""The fleet switch: a turned-off provider or model is never picked by any lane."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT.parent / ".github" / "workflows"
sys.path.insert(0, str(ROOT / ".github/scripts"))
import factory_dispatch_credential_filter as credential_filter  # noqa: E402
import factory_fleet_switch as switch  # noqa: E402
import factory_settings as settings  # noqa: E402
import factory_state_operations as operations  # noqa: E402


def repo_vars(providers: str = "", models: str = "") -> dict[str, str]:
    return {
        settings.REPO_VARS_ENV: json.dumps(
            {switch.PROVIDERS_VAR: providers, switch.MODELS_VAR: models}
        )
    }


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location(
        "factory_work_controller", ROOT / ".github/scripts/factory-work-controller.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "factory_work_controller", module)
    spec.loader.exec_module(module)
    return module


def test_switch_variables_are_delivered_to_every_workflow():
    assert settings.SWITCHES[:2] == (switch.PROVIDERS_VAR, switch.MODELS_VAR)
    assert switch.REASONS_VAR not in settings.workflow_env_value()
    for name in settings.SWITCHES:
        assert f'"{name}": "${{{{ vars.{name} }}}}"' in settings.workflow_env_value()


def test_lists_parse_aliases_and_drop_values_that_could_break_the_payload():
    assert switch.parse_providers('opencode-go, Kilo-Auto,bad"name') == [
        "opencode-free",
        "kilo-auto",
    ]
    assert switch.parse_models('opencode-go=opencode-go/hy4,x=y"z') == [
        ("opencode-free", "opencode-go/hy4")
    ]
    assert switch.format_models([("b", "m"), ("a", "n/m:free")]) == "a=n/m:free,b=m"


def test_roster_filter_skips_workers_of_a_disabled_provider(monkeypatch):
    sources = {"1": "kilo-auto", "2": "nvidia"}
    env = {"FACTORY_HAS_NVIDIA_API_KEY": "true", **repo_vars("kilo-auto")}
    kept, deferred = credential_filter.filter_workers(["1", "2"], sources, env)
    assert kept == ["2"]
    assert "kilo-auto is turned off" in deferred[0]
    kept, _ = credential_filter.filter_workers(
        ["1", "2"], sources, {"FACTORY_HAS_NVIDIA_API_KEY": "true"}
    )
    assert kept == ["1", "2"]


def test_worker_skip_reason_holds_under_force_but_not_when_the_provider_is_named(
    controller,
    monkeypatch,
):
    monkeypatch.setattr(controller, "worker_sources", lambda: {"7": "kilo-auto"})
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda worker: False)
    monkeypatch.setenv(settings.REPO_VARS_ENV, repo_vars("kilo-auto")[settings.REPO_VARS_ENV])
    reason = controller.worker_skip_reason("7", operator_force=True)
    assert reason is not None and "kilo-auto is turned off" in reason
    assert controller.worker_skip_reason("7", operator_force=True, named=True) is None
    monkeypatch.setenv(settings.REPO_VARS_ENV, repo_vars()[settings.REPO_VARS_ENV])
    assert controller.worker_skip_reason("7", operator_force=True) is None


def test_disabled_models_join_exclusions_in_every_catalog_form():
    env = repo_vars(models="gemini-free=google/gemini-2.5-flash,opencode-go=opencode-go/hy4")
    merged = switch.merge_exclusions({"opencode-free": ["old"]}, env)
    assert merged["gemini-free"] == ["gemini-2.5-flash", "google/gemini-2.5-flash"]
    assert merged["opencode-free"] == ["old", "hy4", "opencode-go/hy4"]
    explicit = switch.merge_exclusions({}, env, requested_model="opencode-go/hy4")
    assert "opencode-free" not in explicit


def test_trusted_exports_apply_the_switch_with_and_without_the_store(tmp_path, monkeypatch):
    from tests import turso_support as support

    monkeypatch.setenv(
        settings.REPO_VARS_ENV,
        repo_vars("kilo-auto", "opencode-go=opencode-go/hy4")[settings.REPO_VARS_ENV],
    )
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    monkeypatch.delenv("FACTORY_STATE_REQUIRED", raising=False)
    assert operations.export_model_exclusions("acme/widgets") == {
        "opencode-free": ["hy4", "opencode-go/hy4"]
    }
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (_, repo):
        support.use_roster(monkeypatch, {"7": ("kilo-auto", "")})
        sources = operations.export_fleet_failover_sources(repo, "7")
        assert "kilo-auto" not in sources and "nvidia" in sources
        assert operations.export_model_exclusions(repo)["opencode-free"]


def test_check_command_exits_nonzero_for_a_disabled_lane():
    env = {**os.environ, **repo_vars("local-ollama")}
    script = str(ROOT / ".github/scripts/factory_fleet_switch.py")
    off = subprocess.run(
        [sys.executable, script, "check", "local-ollama"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    on = subprocess.run(
        [sys.executable, script, "check", "kilo-auto"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert off.returncode == 1 and "local-ollama is turned off" in off.stderr
    assert on.returncode == 0


def test_local_ollama_review_obeys_the_switch_on_every_trigger():
    text = (WORKFLOWS / "local-ollama-review.yml").read_text()
    guard, review = text.split("- name: Run provider-backed controller review", 1)
    assert "factory_fleet_switch.py check local-ollama" in guard
    assert "if: steps.switch.outputs.enabled == 'true'" in review.split("run:", 1)[0]


@pytest.mark.parametrize(
    ("disabled", "expected"),
    [
        ("", {"routeway": "true", "requesty": "true", "orcarouter": "true"}),
        ("orcarouter,routeway", {"routeway": "false", "requesty": "true", "orcarouter": "false"}),
    ],
)
def test_pr_agent_review_skips_disabled_providers(tmp_path, disabled, expected):
    if not shutil.which("jq"):
        pytest.skip("jq is not installed in this environment")
    text = (WORKFLOWS / "pr-agent-review.yml").read_text()
    step = text.split("- name: Detect configured free providers", 1)[1].split("run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in step.split("\n\n", 1)[0].splitlines())
    output = tmp_path / "out"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "ROUTEWAY_API_KEY": "k",
        "REQUESTY_API_KEY": "k",
        "ORCAROUTER_API_KEY": "k",
        "FACTORY_REPO_VARS": json.dumps({switch.PROVIDERS_VAR: disabled}),
    }
    subprocess.run(["bash", "-c", script], env=env, check=True)
    assert dict(line.split("=") for line in output.read_text().split()) == expected
