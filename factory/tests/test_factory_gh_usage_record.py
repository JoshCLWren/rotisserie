"""Dispatcher and drain jobs record their GitHub requests and budget for `baste`."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
USAGE = importlib.import_module("factory_gh_usage")
core = importlib.import_module("factory_turso_core")
WORKFLOWS = ROOT.parent / ".github/workflows"
METER = ROOT / ".github/scripts/factory-gh-meter.sh"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(tmp_path / "store.sqlite"))
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    core.use_store(None)
    yield core.store()
    core.use_store(None)


def test_requests_split_by_the_bucket_they_spend() -> None:
    usage = USAGE.summarize([
        "1\tapi GET repos/R/pulls/N\treader", "2\tapi GET repos/R/pulls/N\treader",
        "3\tapi PUT repos/R/issues/N/labels\tworkflow", "4\tcache-hit",
        "5\tapi GET repos/R/issues\t",
    ])
    assert usage["github"] == 3
    assert usage["buckets"] == {"workflow": 2, "reader": 1}


def test_the_meter_tags_reads_that_use_the_read_only_app(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    env = {**os.environ, "FACTORY_GH_CALL_LOG": str(log), "FACTORY_REAL_GH": str(shutil.which("true")),
           "FACTORY_READ_TOKEN": "t", "FACTORY_READ_TOKEN_EXPIRES": "9999999999"}
    for args in (["api", "repos/o/r/issues/1"], ["api", "-X", "PUT", "repos/o/r/issues/1/labels"],
                 ["api", "rate_limit"]):
        subprocess.run(["bash", str(METER), *args], check=True, env=env)
    assert [line.split("\t")[2] for line in log.read_text().splitlines()] == [
        "reader", "workflow", "workflow",
    ]


def test_a_recorded_job_is_exported_with_its_budget(store: object, tmp_path: Path) -> None:
    rest = tmp_path / "rate.json"
    rest.write_text(json.dumps({"resources": {"core": {"remaining": 1806, "limit": 5000, "reset": 1}}}))
    usage = USAGE.summarize(["1\tapi GET repos/R/pulls/N\treader"])
    for _ in range(USAGE.KEEP + 3):
        USAGE.record("acme/widgets", "dispatch roster", usage, USAGE.budget(rest))
    USAGE.record("acme/widgets", "worker 7", usage, None)
    exported = USAGE.export("acme/widgets")
    assert set(exported) == {"dispatch"}
    assert len(exported["dispatch"]) == USAGE.KEEP
    assert exported["dispatch"][-1]["budget"] == {"remaining": 1806, "limit": 5000, "reset": 1}
    assert exported["dispatch"][-1]["buckets"] == {"workflow": 0, "reader": 1}


def test_an_unreadable_budget_is_left_out(tmp_path: Path) -> None:
    assert USAGE.budget(None) is None
    bad = tmp_path / "rate.json"
    bad.write_text("not json")
    assert USAGE.budget(bad) is None


def _run(workflow: str, job: str, name: str) -> str:
    steps = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][job]["steps"]
    return str(next(step for step in steps if step.get("name") == name)["run"])


def test_dispatch_and_drain_record_usage_and_status_pages_publish_it() -> None:
    for workflow, job in (("fixed-model-factory-dispatch.yml", "dispatch"),
                          ("factory-ready-merge-drain.yml", "merge-ready")):
        run = _run(workflow, job, "Report GitHub CLI request usage")
        assert '--record "$GITHUB_REPOSITORY"' in run and '--rate-limit "$rest"' in run
    run = _run("factory-status-pages.yml", "publish-live-dashboard", "Add GitHub request usage to the published stats")
    assert "factory_gh_usage.py export" in run and ".gh_usage = " in run
