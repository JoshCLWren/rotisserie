from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rotisserie.cli import EXIT_INVALID, EXIT_OK, EXIT_REJECTED, main
from rotisserie.operator.controller import review_view_from_dict

EXAMPLE = Path(__file__).parents[1] / "examples" / "controller" / "review"


def test_public_review_plan_records_exact_inputs_without_graph_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shutil.copytree(EXAMPLE, tmp_path, dirs_exist_ok=True)
    args = [
        "--config",
        str(tmp_path / "config.toml"),
        "controller-review-plan",
        "--view",
        str(tmp_path / "view.json"),
        "--assignment",
        str(tmp_path / "assignment.json"),
        "--operation",
        "review-demo",
        "--lease-id",
        "review-lease",
        "--at",
        "10",
        "--max-observation-age",
        "0",
    ]
    assert main(args) == EXIT_OK
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "selected"
    evidence = output["evidence"]
    assert (
        evidence["view_sha256"] == hashlib.sha256((tmp_path / "view.json").read_bytes()).hexdigest()
    )
    assert (
        evidence["assignment_sha256"]
        == hashlib.sha256((tmp_path / "assignment.json").read_bytes()).hexdigest()
    )
    assert evidence["result"]["binding"]["candidate"]["revision"] == "a" * 40
    assert not (tmp_path / ".rotisserie-state" / "graph.json").exists()
    assert not (tmp_path / "unused.json").exists()
    raw = json.loads((tmp_path / "view.json").read_text())
    raw["candidates"][0]["complete"] = False
    (tmp_path / "view.json").write_text(json.dumps(raw))
    assert main(args) == EXIT_REJECTED
    assert json.loads(capsys.readouterr().out)["status"] == "provenance_hold"
    raw["candidates"][0]["complete"] = "true"
    (tmp_path / "view.json").write_text(json.dumps(raw))
    assert main(args) == EXIT_INVALID
    capsys.readouterr()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("liveness_complete", "true"),
        ("bindings", None),
        ("candidates", None),
    ],
)
def test_review_envelope_rejects_malformed_fields(field: str, value: object) -> None:
    raw = json.loads((EXAMPLE / "view.json").read_text())
    raw[field] = value
    with pytest.raises(ValueError):
        review_view_from_dict(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("producer_attested", 1),
        ("observed_at", True),
        ("contributors", None),
        ("producer", "unknown"),
        ("provenance_recorded", "true"),
    ],
)
def test_candidate_decoder_refuses_ambiguous_provenance(field: str, value: object) -> None:
    raw = json.loads((EXAMPLE / "view.json").read_text())
    raw["candidates"][0][field] = value
    with pytest.raises(ValueError):
        review_view_from_dict(raw)


def test_duplicate_candidates_and_contributors_rejected() -> None:
    raw: dict[str, Any] = json.loads((EXAMPLE / "view.json").read_text())
    raw["candidates"].append(raw["candidates"][0])
    with pytest.raises(ValueError):
        review_view_from_dict(raw)
    raw["candidates"].pop()
    raw["candidates"][0]["contributors"].append(raw["candidates"][0]["contributors"][0])
    with pytest.raises(ValueError):
        review_view_from_dict(raw)


def test_durable_binding_roundtrip_requires_full_exact_lease_and_assignment() -> None:
    from rotisserie.application.process import assignment_from_dict
    from rotisserie.application.review_controller import ReviewRequest, plan_review
    from rotisserie.domain import LeaseId, SchedulingPolicy

    raw = json.loads((EXAMPLE / "view.json").read_text())
    view = review_view_from_dict(raw)
    assignment = assignment_from_dict(json.loads((EXAMPLE / "assignment.json").read_text()))
    request = ReviewRequest(
        assignment.authority.work.repository,
        assignment,
        LeaseId("lease"),
        "roundtrip",
        SchedulingPolicy(3),
        10,
        0,
        frozenset(x.id for x in view.controller.graph.snapshot.workers),
    )
    binding = plan_review(view, request).binding
    assert binding is not None
    raw["controller"]["snapshot"] = replace(
        view.controller.graph.snapshot, leases=(binding.lease,)
    ).to_dict()
    raw["controller"]["active"] = 1
    raw["bindings"] = [binding.to_dict()]
    assert review_view_from_dict(raw).bindings == (binding,)
    raw["bindings"][0]["lease"]["expires_at"] = 40
    with pytest.raises(ValueError):
        review_view_from_dict(raw)
    raw["bindings"][0] = binding.to_dict()
    raw["bindings"][0]["assignment"]["authority"]["revision"]["value"] = "b" * 40
    with pytest.raises(ValueError):
        review_view_from_dict(raw)
