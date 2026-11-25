"""An operator-forced run uses its requested model past the factory's own exclusions.

The 2026-11-25 ComicPile case: `baste run opencode-go opencode-go/hungerg-free
--force --exact --target pr:5772` (run 45616082736) failed with "Exact requested
model could not be selected" because one transient "Model unavailable" line had
benched hungerg-free for six hours.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import factory_forced_model as forced  # noqa: E402
import factory_state_operations as operations  # noqa: E402

from tests import turso_support as support  # noqa: E402

MODEL = "vendor/hot"
REQUESTED = "nvidia/" + MODEL


@pytest.fixture
def store(tmp_path, monkeypatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        support.use_roster(monkeypatch, {"7": ("nvidia", "")})
        yield db, repo


def test_a_forced_run_skips_health_and_backoff_exclusions(store):
    db, repo = store
    support.health(db, repo, "nvidia", MODEL, "unavailable", 3 * 3600, outcome="model_unavailable")
    support.backoff(
        db,
        repo,
        "model",
        source="nvidia",
        model=MODEL,
        outcome="work_failure",
        streak=3,
        retry_in=600,
    )
    assert MODEL in operations.export_model_exclusions(repo, "7", REQUESTED)["nvidia"]
    found, note = forced.forced_exclusions(repo, "7", REQUESTED)
    assert MODEL not in found.get("nvidia", [])
    assert note.startswith("forced past health exclusion: nvidia/vendor/hot")
    assert "model_unavailable" in note and "backoff streak 3" in note


def test_a_forced_run_leaves_other_models_excluded(store):
    db, repo = store
    for model in (MODEL, "vendor/other"):
        support.health(db, repo, "nvidia", model, "unavailable", 3600, outcome="model_unavailable")
    found, _ = forced.forced_exclusions(repo, "7", REQUESTED)
    assert found["nvidia"] == ["vendor/other"]


@pytest.mark.parametrize("cause", ["quota", "throttle", "account", "retired"])
def test_a_forced_run_still_respects_capacity(store, cause):
    db, repo = store
    if cause == "account":
        support.backoff(db, repo, "provider", source="nvidia", outcome="provider_quota")
        support.health(db, repo, "nvidia", MODEL, "unavailable", 3600, outcome="model_unavailable")
    elif cause == "retired":
        support.health(db, repo, "nvidia", MODEL, "retired", None, outcome="model_retired_410")
    else:
        last = "provider_quota" if cause == "quota" else "provider_throttle"
        support.health(db, repo, "nvidia", MODEL, "cooling", 3600, outcome=last)
    found, note = forced.forced_exclusions(repo, "7", REQUESTED)
    assert MODEL in found["nvidia"]
    assert note.startswith("forced run kept vendor/hot excluded")


def test_the_cli_prints_the_note_only_when_forced(store, monkeypatch, capsys):
    db, repo = store
    support.health(db, repo, "nvidia", MODEL, "unavailable", 3600, outcome="model_unavailable")
    base = ["x", "export-model-exclusions", "--repo", repo, "--worker", "7"]
    for forced_flag, expect_model in (("false", True), ("true", False)):
        argv = [*base, "--requested-model", REQUESTED, "--operator-forced", forced_flag]
        monkeypatch.setattr(sys, "argv", argv)
        operations.main()
        out = capsys.readouterr()
        assert (MODEL in json.loads(out.out).get("nvidia", [])) is expect_model
        assert ("forced past health exclusion" in out.err) is not expect_model


@pytest.fixture
def controller():
    spec = importlib.util.spec_from_file_location(
        "forced_controller", ROOT / ".github/scripts/factory-work-controller.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("operator_claim", [True, False])
def test_only_a_verified_operator_claim_marks_the_assignment_forced(
    controller, monkeypatch, operator_claim
):
    assignment = {
        "kind": "pr",
        "number": 5772,
        "worker": "54",
        "claim_id": "c",
        "head": "a" * 40,
        "branch": "b",
        "stage": "factory:review",
        "operator_forced": True,
    }
    monkeypatch.setattr(controller, "worker_claim", lambda *_, **__: ("c", [5772]))
    monkeypatch.setattr(
        controller, "target_json", lambda _: {"state": "open", "labels": [{"name": "factory:54"}]}
    )
    monkeypatch.setattr(
        controller,
        "live_pr_state",
        lambda _: {"head": {"sha": "a" * 40, "ref": "b"}, "labels": [{"name": "factory:review"}]},
    )
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _: True)
    monkeypatch.setattr(controller, "operator_forced_claim", lambda *_: operator_claim)
    found = controller.validate_assignment("54", json.dumps(assignment))
    assert found["operator_forced"] is operator_claim


def test_the_run_workflow_passes_the_verified_flag_and_records_the_bypass():
    text = (REPO_ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    assert '--operator-forced "$operator_forced"' in text
    assert "if .operator_forced == true" in text
    assert "Operator-forced model" in text


@pytest.fixture
def pair(tmp_path, monkeypatch):
    with support.bound("file", tmp_path / "leases.sqlite", monkeypatch) as (db, repo):
        support.use_roster(monkeypatch, {"7": ("nvidia", ""), "8": ("nvidia", "")})
        yield db, repo


def test_an_explicit_claim_skips_the_workers_own_backoff(pair):
    import factory_turso_leases as leases

    db, repo = pair
    support.backoff(
        db,
        repo,
        "worker",
        source="nvidia",
        worker="7",
        outcome="work_failure",
        streak=5,
        retry_in=3600,
    )
    assert leases.acquire_lease(repo, "7", [5772]) is None
    assert leases.acquire_lease(repo, "7", [5772], explicit=True)


def test_an_explicit_claim_still_respects_a_provider_account_cooldown(pair):
    import factory_turso_leases as leases

    db, repo = pair
    support.backoff(db, repo, "provider", source="nvidia", outcome="provider_quota")
    assert leases.acquire_lease(repo, "7", [5772], explicit=True) is None
    with pytest.raises(RuntimeError, match="provider account nvidia cooling"):
        leases.forced_refusal(repo, "7", [5772])


def test_a_forced_claim_on_a_held_target_names_the_holder_and_run(pair, monkeypatch):
    import factory_turso_leases as leases

    _, repo = pair
    monkeypatch.setenv("GITHUB_RUN_ID", "45616357959")
    assert leases.acquire_lease(repo, "8", [5746])
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    assert leases.acquire_lease(repo, "7", [5746], explicit=True) is None
    with pytest.raises(
        RuntimeError, match=r"busy: #5746 is held by worker 8 \(claim \w{8}, run 45616357959\)"
    ):
        leases.forced_refusal(repo, "7", [5746])
