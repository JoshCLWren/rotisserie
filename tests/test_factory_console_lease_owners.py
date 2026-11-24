"""`baste leases` shows which Actions run owns each worker claim and whether it still runs."""

from __future__ import annotations

import importlib
import io
import sys
import types
from pathlib import Path

import pytest

from rotisserie.factory_console.commands import prs
from rotisserie.factory_console.service import sync
from rotisserie.factory_console.ui import Console

SCRIPTS = Path(__file__).resolve().parents[1] / "factory" / ".github" / "scripts"
REPO = "acme/widgets"


class Hub:
    repo = REPO

    def __init__(self, runs: dict[str, dict[str, object]]) -> None:
        self.runs = runs
        self.paths: list[str] = []

    def repo_api(self, path: str, **_kwargs: object) -> object:
        self.paths.append(path)
        return self.runs.get(path.rsplit("/", 1)[-1], {})


@pytest.fixture
def owners_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("factory_lease_owners", None)
    return importlib.import_module("factory_lease_owners")


def test_lease_report_names_the_owning_run_and_its_state(
    owners_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim = "0a0cd34f-0000-4000-8000-000000000000"
    leases = [
        {"key": f"factory-lease-v1:{REPO}:worker:1244980533700", "worker": "1244980533700",
         "claim_id": claim, "active": True, "expires_at": "2026-11-24T23:07:08+00:00",
         "acquired_at": "2026-11-24T19:30:20+00:00", "run_id": "45470929112"},
        {"key": f"factory-lease-v1:{REPO}:target:5756", "worker": "1244980533700",
         "claim_id": claim, "active": True, "expires_at": "2026-11-24T23:07:08+00:00"},
    ]  # fmt: skip
    ledger = types.SimpleNamespace(
        list_leases=lambda repo: leases, claim_owners=owners_module.claim_owners
    )
    monkeypatch.setattr(sync, "load_ledger", lambda env=None: (ledger, ""))
    hub = Hub({"45470929112": {"id": 45470929112, "status": "completed"}})
    found = sync.lease_report(hub, owners=True)  # type: ignore[arg-type]
    assert {row.owner_run for row in found.rows} == {"run 45470929112 (recorded), completed"}
    report = found.to_dict()
    assert hub.paths == ["actions/runs/45470929112"]
    stream = io.StringIO()
    prs._show_leases(Console(stream), report)
    assert "Owning run" in stream.getvalue() and "45470929112" in stream.getvalue()


def test_lease_report_skips_run_lookups_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object) -> list[object]:
        raise AssertionError("owner lookup without owners=True")

    ledger = types.SimpleNamespace(list_leases=lambda repo: [], claim_owners=refuse)
    monkeypatch.setattr(sync, "load_ledger", lambda env=None: (ledger, ""))
    assert sync.lease_report(Hub({})).rows == ()  # type: ignore[arg-type]
