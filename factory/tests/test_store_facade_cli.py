"""The ledger and operations command lines that workflows call, run against a SQLite store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import turso_support as support

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


@pytest.fixture
def cli(tmp_path: Path):
    path = tmp_path / "state.sqlite"
    repo = support.repo_name()
    env = {**os.environ, "FACTORY_STATE_SQLITE": str(path)}
    env.pop("FACTORY_TURSO_URL", None)
    env.pop("FACTORY_DB_USAGE_FILE", None)

    def run(script: str, *args: str, stdin: str = "") -> str:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args, "--repo", repo],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    return run, path, repo


def test_bootstrap_creates_the_schema(cli) -> None:
    run, path, _ = cli
    assert run("factory_state_operations.py", "bootstrap").startswith(
        "[factory-operations] schema ready (version "
    )
    store = support.libsql.FileStore(path)
    assert store.read("SELECT count(*) AS n FROM factory_lease") == [{"n": 0}]


def test_lifecycle_records_one_event_per_run_and_status(cli) -> None:
    run, path, repo = cli
    worker = support.uncapped_workers(1)[0]
    args = ("lifecycle", "--run-id", "8801", "--worker", worker, "--status", "started")
    assert run("factory_state_ledger.py", *args) == "[factory-ledger] recorded 1 lifecycle event(s)"
    assert run("factory_state_ledger.py", *args) == "[factory-ledger] recorded 0 lifecycle event(s)"
    rows = support.libsql.FileStore(path).read(
        "SELECT event_type, event_id FROM factory_state_events WHERE repo = :r", {"r": repo}
    )
    assert rows == [{"event_type": "worker_started", "event_id": "lifecycle:8801:started"}]


def test_exports_print_their_json_shapes(cli) -> None:
    run, _, _ = cli
    health = json.loads(run("factory_state_operations.py", "export-candidate-health"))
    assert health == {"models": [], "providers": []}
    backoffs = json.loads(run("factory_state_operations.py", "export-backoffs"))
    assert "providers" in backoffs and backoffs["providers"] == {}


def test_filter_reads_workers_from_stdin_and_prints_the_kept_list(cli) -> None:
    run, _, _ = cli
    workers = support.uncapped_workers(2)
    kept = json.loads(run("factory_state_operations.py", "filter", stdin=json.dumps(workers)))
    assert kept == workers


def test_prune_prints_both_retention_counts(cli) -> None:
    run, _, _ = cli
    line = run("factory_state_operations.py", "prune")
    assert line.startswith("[factory-store] ")
    assert json.loads(line.removeprefix("[factory-store] ")) == {
        "lifecycle_events_pruned": 0,
        "model_attempts_rolled_up": 0,
    }
