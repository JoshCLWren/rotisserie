from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from rotisserie.cli import EXIT_INVALID, EXIT_OK, EXIT_REJECTED, main
from rotisserie.operator.controller import controller_view_from_dict

EXAMPLE = Path(__file__).parents[1] / "examples" / "controller"


def test_fixture_cli_records_digests_without_initializing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shutil.copytree(EXAMPLE, tmp_path, dirs_exist_ok=True)
    args = [
        "--config",
        str(tmp_path / "config.toml"),
        "controller-plan",
        "--view",
        str(tmp_path / "view.json"),
        "--assignment",
        str(tmp_path / "assignment.json"),
        "--operation",
        "demo-tick-1",
        "--lease-id",
        "demo-lease-1",
        "--at",
        "10",
        "--max-observation-age",
        "0",
    ]
    assert main(args) == EXIT_OK
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "selected"
    assert output["evidence"]["result"]["selected"] == ["1"]
    assert len(output["evidence"]["view_sha256"]) == 64
    assert not (tmp_path / "unused.json").exists()
    assert not (tmp_path / ".rotisserie-state" / "graph.json").exists()
    view_path = tmp_path / "view.json"
    view = json.loads(view_path.read_text())
    view["liveness"] = []
    view_path.write_text(json.dumps(view))
    assert main(args) == EXIT_REJECTED
    assert json.loads(capsys.readouterr().out)["status"] == "liveness_unknown"
    view["active"] = None
    view_path.write_text(json.dumps(view))
    assert main(args) == EXIT_INVALID
    capsys.readouterr()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("active", True),
        ("active", -1),
        ("completion_backlog", None),
        ("liveness", [{"namespace": "demo", "value": "one", "state": "bad"}]),
    ],
)
def test_acquisition_envelope_rejects_untrustworthy_observations(field: str, value: object) -> None:
    raw = json.loads((EXAMPLE / "view.json").read_text())
    raw[field] = value
    with pytest.raises(ValueError):
        controller_view_from_dict(raw)


def test_recovery_cli_plans_and_holds_without_mutable_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shutil.copytree(EXAMPLE, tmp_path, dirs_exist_ok=True)
    args = [
        "--config",
        str(tmp_path / "config.toml"),
        "controller-recovery-plan",
        "--view",
        str(tmp_path / "recovery-view.json"),
        "--operation",
        "recover-demo",
        "--at",
        "20",
        "--max-observation-age",
        "0",
        "--inactivity-ttl",
        "10",
    ]
    assert main(args) == EXIT_OK
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "recovery_planned"
    assert output["evidence"]["result"]["lease"]["id"] == "expired-demo"
    assert len(output["evidence"]["view_sha256"]) == 64
    assert not (tmp_path / ".rotisserie-state" / "graph.json").exists()
    raw = json.loads((tmp_path / "recovery-view.json").read_text())
    raw["liveness_complete"] = False
    (tmp_path / "recovery-view.json").write_text(json.dumps(raw))
    assert main(args) == EXIT_REJECTED
    assert json.loads(capsys.readouterr().out)["status"] == "liveness_hold"
    raw["activity"].append(raw["activity"][0])
    (tmp_path / "recovery-view.json").write_text(json.dumps(raw))
    assert main(args) == EXIT_INVALID
    capsys.readouterr()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("liveness_complete", None),
        ("liveness_complete", "true"),
        ("activity", None),
    ],
)
def test_recovery_envelope_rejects_malformed_fields(field: str, value: object) -> None:
    from rotisserie.operator.controller import recovery_view_from_dict

    raw = json.loads((EXAMPLE / "recovery-view.json").read_text())
    raw[field] = value
    with pytest.raises(ValueError):
        recovery_view_from_dict(raw)


def test_recovery_envelope_rejects_boolean_exact_lease_timestamp() -> None:
    from rotisserie.operator.controller import recovery_view_from_dict

    raw = json.loads((EXAMPLE / "recovery-view.json").read_text())
    raw["activity"][0]["lease"]["acquired_at"] = True
    with pytest.raises(ValueError):
        recovery_view_from_dict(raw)
