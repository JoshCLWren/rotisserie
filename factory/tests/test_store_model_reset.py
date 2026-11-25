"""Clearing one model's health bench and backoffs, and the workflow that runs it."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path

import pytest
import yaml

from tests import turso_support as support

reset = importlib.import_module("factory_turso_model_reset")
FACTORY = Path(__file__).resolve().parents[1]
ROOT = FACTORY.parent


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def test_only_the_named_model_is_cleared(env) -> None:
    db, repo = env
    support.health(db, repo, "nvidia", "kimi", "cooling", 1200)
    support.health(db, repo, "groq", "kimi", "cooling", 1200)
    support.health(db, repo, "nvidia", "other", "cooling", 1200)
    support.backoff(db, repo, "model", source="nvidia", model="kimi", streak=5, retry_in=60)
    support.backoff(db, repo, "model", source="nvidia", model="other", retry_in=60)
    support.backoff(db, repo, "worker", worker="41", source="nvidia", model="kimi", retry_in=60)
    support.backoff(db, repo, "provider", source="nvidia", outcome="provider_quota")
    found = reset.reset_one_model(repo, "kimi", "nvidia")
    assert [(r["source"], r["model"]) for r in found["model_health_rows"]] == [("nvidia", "kimi")]
    assert [r["streak"] for r in found["backoff_rows"]] == [5]
    assert found["worker_backoffs_kept"] == 1
    left = db.read(
        "SELECT source, model FROM factory_model_health WHERE repo = :r ORDER BY 1, 2", {"r": repo}
    )
    assert left == [{"source": "groq", "model": "kimi"}, {"source": "nvidia", "model": "other"}]
    kinds = db.read(
        "SELECT kind, model FROM factory_backoff WHERE repo = :r ORDER BY 1, 2", {"r": repo}
    )
    assert kinds == [
        {"kind": "model", "model": "other"},
        {"kind": "provider", "model": ""},
        {"kind": "worker", "model": "kimi"},
    ]
    assert db.read("SELECT * FROM factory_repo_state WHERE repo = :r", {"r": repo}) == []
    every_source = reset.reset_one_model(repo, "kimi")
    assert [r["source"] for r in every_source["model_health_rows"]] == ["groq"]
    assert reset.reset_one_model(repo, "kimi")["model_health_rows"] == []


@pytest.mark.parametrize("model", ["", "a b", "x" * 181])
def test_a_bad_model_is_refused(model: str) -> None:
    with pytest.raises(ValueError):
        reset.reset_one_model("acme/widgets", model)


WIDENED = [
    {"model": "m", "quota_only": True},
    {"model": "m", "include_retired": True},
    {"source": "nvidia"},
]


@pytest.mark.parametrize("extra", WIDENED)
def test_the_command_line_never_widens_a_one_model_reset(
    extra: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = importlib.import_module("factory_state_operations")

    def forbidden(*args: object) -> None:
        raise AssertionError("a wider reset ran")

    monkeypatch.setattr(operations, "enabled", lambda: True)
    for name in ("reset_model_health", "reset_quota_backoffs", "reset_one_model"):
        monkeypatch.setattr(operations, name, forbidden)
    values = {"model": "", "source": "", "quota_only": False, "include_retired": False, **extra}
    args = argparse.Namespace(command="reset-model-health", repo="acme/w", **values)
    with pytest.raises(SystemExit, match="model"):
        operations._store_command(args)


def test_the_workflow_checks_one_model_before_any_full_reset() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/factory-model-health-reset.yml").read_text()
    )
    assert set(workflow[True]["workflow_call"]["inputs"]) >= {"model", "source"}
    step = next(
        s for s in workflow["jobs"]["reset"]["steps"] if s.get("name", "").startswith("Clear")
    )
    script = step["run"]
    assert step["env"]["MODEL"] == "${{ inputs.model }}"
    assert "${{" not in script
    assert script.index('if [[ -n "$MODEL" || -n "$SOURCE" ]]') < script.index("--include-retired")
    assert script.index('args+=(--model "$MODEL")') < script.index("--quota-only")
    caller = yaml.safe_load(
        (FACTORY / "install/callers/factory-model-health-reset.yml").read_text()
    )
    assert caller["jobs"]["rotisserie"]["with"]["model"] == "${{ inputs.model }}"
    assert caller[True]["workflow_dispatch"]["inputs"]["model"]["default"] == ""
    overlay = (FACTORY / "install/overlay-paths.txt").read_text().split()
    assert ".github/scripts/factory_turso_model_reset.py" in overlay
