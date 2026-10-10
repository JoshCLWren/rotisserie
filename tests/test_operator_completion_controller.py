from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rotisserie.application.completion_controller import (
    CompletionPhase,
    CompletionRecord,
    plan_completion,
)
from rotisserie.cli import EXIT_INVALID, EXIT_OK, EXIT_REJECTED, main
from rotisserie.operator.completion import completion_request_from_dict, completion_view_from_dict

EXAMPLE = Path(__file__).parents[1] / "examples/controller/completion"


def test_public_planning_cli_records_inputs_and_never_initializes_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copytree(EXAMPLE, tmp_path, dirs_exist_ok=True)
    args = [
        "--config",
        str(tmp_path / "config.toml"),
        "controller-completion-plan",
        "--view",
        str(tmp_path / "view.json"),
        "--request",
        str(tmp_path / "request.json"),
    ]
    assert main(args) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "selected"
    for name in ("view", "request"):
        assert (
            out["evidence"][name + "_sha256"]
            == hashlib.sha256((tmp_path / (name + ".json")).read_bytes()).hexdigest()
        )
    assert out["evidence"]["result"]["binding"]["evidence"]["provenance"]["revision"] == "a" * 40
    assert not (tmp_path / ".rotisserie-state/graph.json").exists()
    assert not (tmp_path / "unused.json").exists()
    raw = json.loads((tmp_path / "view.json").read_text())
    raw["candidates"][0]["ci"] = []
    (tmp_path / "view.json").write_text(json.dumps(raw))
    assert main(args) == EXIT_REJECTED
    assert json.loads(capsys.readouterr().out)["status"] == "ci_hold"
    raw["candidates"][0]["trusted"] = "true"
    (tmp_path / "view.json").write_text(json.dumps(raw))
    assert main(args) == EXIT_INVALID
    capsys.readouterr()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("liveness_complete", "true"),
        ("candidates", None),
        ("records", None),
    ],
)
def test_malformed_envelope(field: str, value: Any) -> None:
    raw = json.loads((EXAMPLE / "view.json").read_text())
    raw[field] = value
    with pytest.raises(ValueError):
        completion_view_from_dict(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("at", True),
        ("lease_ttl", 0),
        ("max_observation_age", -1),
        ("required_ci", []),
        ("required_readiness", []),
        ("workers", []),
        ("schema_version", 2),
        ("wip_limit", "3"),
        ("operation", "bearer credential"),
    ],
)
def test_malformed_authority(field: str, value: Any) -> None:
    raw = json.loads((EXAMPLE / "request.json").read_text())
    raw[field] = value
    with pytest.raises(ValueError):
        completion_request_from_dict(raw)


@pytest.mark.parametrize(
    "lane,field,value",
    [
        ("ci", "status", "unknown"),
        ("readiness", "required", "true"),
        ("reviews", "decision", "unknown"),
        ("reviews", "reviewer", None),
    ],
)
def test_unknown_evidence_never_becomes_pass(lane: str, field: str, value: Any) -> None:
    raw = json.loads((EXAMPLE / "view.json").read_text())
    raw["candidates"][0][lane][0][field] = value
    with pytest.raises(ValueError):
        completion_view_from_dict(raw)


@pytest.mark.parametrize("lane", ["ci", "readiness", "reviews"])
def test_ambiguous_duplicate_evidence(lane: str) -> None:
    raw = json.loads((EXAMPLE / "view.json").read_text())
    raw["candidates"][0][lane].append(raw["candidates"][0][lane][0])
    with pytest.raises(ValueError):
        completion_view_from_dict(raw)


@pytest.mark.parametrize("phase", list(CompletionPhase))
def test_durable_record_roundtrip_including_uncertain_and_completed(phase: CompletionPhase) -> None:
    raw = json.loads((EXAMPLE / "view.json").read_text())
    view = completion_view_from_dict(raw)
    request = completion_request_from_dict(json.loads((EXAMPLE / "request.json").read_text()))
    binding = plan_completion(view, request).binding
    assert binding
    record = CompletionRecord(binding, phase)
    raw["controller"]["snapshot"] = replace(
        view.controller.graph.snapshot, leases=(binding.lease,)
    ).to_dict()
    raw["controller"]["active"] = 1
    raw["records"] = [record.to_dict()]
    assert completion_view_from_dict(raw).records == (record,)
    raw["records"][0]["binding"]["request"]["revision"] = "b" * 40
    with pytest.raises(ValueError):
        completion_view_from_dict(raw)


def test_cli_rejects_repository_or_worker_inventory_confusion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copytree(EXAMPLE, tmp_path, dirs_exist_ok=True)
    args = [
        "--config",
        str(tmp_path / "config.toml"),
        "controller-completion-plan",
        "--view",
        str(tmp_path / "view.json"),
        "--request",
        str(tmp_path / "request.json"),
    ]
    raw = json.loads((tmp_path / "request.json").read_text())
    raw["repository"]["name"] = "foreign"
    (tmp_path / "request.json").write_text(json.dumps(raw))
    assert main(args) == EXIT_INVALID
    assert "configured repository" in json.loads(capsys.readouterr().out)["error"]
    raw["repository"]["name"] = "repo"
    raw["workers"] = [raw["worker"]]
    (tmp_path / "request.json").write_text(json.dumps(raw))
    assert main(args) == EXIT_INVALID
    capsys.readouterr()
