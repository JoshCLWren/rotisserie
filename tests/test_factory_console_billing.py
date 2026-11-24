"""`rotisserie factory models PROVIDER --billing`: the credit-pool rule the worker applies."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from rotisserie import cli
from rotisserie.factory_console import commands, runtime
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import billing

CATALOG = {
    "data": [
        {"id": "synthetic/paid-tokens", "pricing": {"prompt": "0.00001", "completion": "0.00005"}},
        {"id": "synthetic/paid-request", "pricing": {"request": "0.150000"}},
        {"id": "synthetic/zero", "pricing": {"request": "0.000000"}},
        {"id": "synthetic/router:free"},
    ]
}


def test_pool_membership_reads_a_worker_fleet_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "factory-fleet-catalogs.json"
    snapshot.write_text(json.dumps({"orcarouter": CATALOG, "nvidia": {"data": []}}))
    found = billing.pool_membership(runtime.resolve_provider("orcarouter"), snapshot)
    assert found.paid == ["synthetic/paid-request", "synthetic/paid-tokens"]
    assert found.free == ["synthetic/zero"]
    assert found.unknown == ["synthetic/router:free"]
    assert found.origin.endswith("(orcarouter)")


def test_provider_without_a_public_catalog_needs_a_saved_one() -> None:
    with pytest.raises(billing.CatalogUnavailableError, match="--billing FILE"):
        billing.pool_membership(runtime.resolve_provider("orcarouter"))


class Out(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_models_billing_command_prints_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None, allow_exit_one=False: "[]")
    catalog = tmp_path / "orcarouter.json"
    catalog.write_text(json.dumps(CATALOG))
    arguments = cli.parser().parse_args([
        "factory", "--repo", "acme/widgets", "--json",
        "models", "orcarouter", "--billing", str(catalog),
    ])  # fmt: skip
    out = Out()
    code = commands.main(arguments, env={"TERM": "xterm"}, stdout=out, stdin=Out(), executable="sh")
    data = json.loads(out.getvalue())
    assert code == 0
    assert data["paid"] == ["synthetic/paid-request", "synthetic/paid-tokens"]
    assert data["free"] == ["synthetic/zero"]
    assert data["unknown"] == ["synthetic/router:free"]
