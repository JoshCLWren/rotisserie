from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import socket
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from rotisserie.adapters.github.completion_capture import (
    CompletionCapture,
    compare_completion_capture,
)
from rotisserie.adapters.github.completion_source import (
    SOURCE_SHA,
    SourceDecisions,
    verified_source,
)
from rotisserie.application.completion_controller import CompletionResult, CompletionStatus
from rotisserie.cli import main
from rotisserie.operator.completion import completion_request_from_dict, completion_view_from_dict

EXAMPLE = Path(__file__).parents[1] / "examples/completion-comparison"


@pytest.fixture
def host() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((EXAMPLE / "capture.json").read_bytes()))


def compare(host: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(host).encode()
    capture = CompletionCapture.read(
        raw, digest=hashlib.sha256(raw).hexdigest(), source_sha=SOURCE_SHA
    )
    return compare_completion_capture(
        capture, source_directory=EXAMPLE / "source", source_sha=SOURCE_SHA
    )


def review(
    host: dict[str, Any], *, worker: str = "8", verdict: str = "approve", producer: str = "6"
) -> None:
    item = deepcopy(host["comments"][-1])
    item["id"] = max(x["id"] for x in host["comments"]) + 1
    item["body"] = (
        f"<!-- comic-pile-factory-semantic-review-v1:pr-15:head-{host['pr']['headRefOid']}:"
        f"reviewer-{worker}:producer-{producer}:verdict-{verdict} -->"
    )
    host["comments"].append(item)


def test_actual_pinned_source_and_public_input_share_one_capture(host: dict[str, Any]) -> None:
    result = compare(host)
    assert result["matches"]
    assert result["legacy"]["selected"]
    assert result["rotisserie"]["status"] == "selected"
    assert result["legacy"]["authorization"]["contributors"] == ["6", "7"]
    assert {
        x["value"] for x in result["completion_view"]["candidates"][0]["provenance"]["contributors"]
    } == {"6", "7"}
    assert result["legacy"]["reads"] == [
        "main_health",
        "pr",
        "comments",
        "pr",
        "checks",
        "host_reviews",
        "inline_comments",
    ]
    assert result["legacy"]["expected_revision"] == result["completion_request"]["revision"]
    assert result["source_sha"] == SOURCE_SHA
    assert result["capture_sha256"] in result["completion_view"]["controller"]["version"]
    assert not result["production_authority"] and not result["cutover_authority"]
    # Emitted inputs are consumable by the already-public planning contract.
    completion_view_from_dict(result["completion_view"])
    completion_request_from_dict(result["completion_request"])
    host["active"] = 1
    changed = compare(host)
    assert changed["binding_sha256"] != result["binding_sha256"]
    assert (
        changed["completion_view"]["controller"]["version"]
        != result["completion_view"]["controller"]["version"]
    )


@pytest.mark.parametrize("kind", ["producer", "earlier_repairer"])
def test_full_lineage_prevents_self_review(host: dict[str, Any], kind: str) -> None:
    host["comments"] = host["comments"][:2]
    review(host, worker="6" if kind == "producer" else "7")
    result = compare(host)
    assert result["matches"] and not result["legacy"]["selected"]
    assert result["rotisserie"]["status"] == "review_hold"
    assert result["legacy"]["suppressed_effects"]


@pytest.mark.parametrize("kind", ["missing", "unknown", "unattested", "contradictory"])
def test_source_approval_shortcuts_never_confer_mutation_authority(
    host: dict[str, Any], kind: str
) -> None:
    if kind == "missing":
        host["comments"] = host["comments"][2:]
        review(host, worker="9")
    elif kind == "unknown":
        host["pr"]["headRefName"] = "feature/unknown"
        host["comments"][-1]["body"] = host["comments"][-1]["body"].replace(
            "producer-6", "producer-unknown"
        )
    elif kind == "unattested":
        host["comments"][-1]["body"] = host["comments"][-1]["body"].replace(
            "producer-6", "producer-unknown"
        )
    else:
        review(host, worker="9", producer="7")
    result = compare(host)
    assert result["legacy"]["selected"] and result["rotisserie"]["status"] == "provenance_hold"
    assert result["deliberate_holds"] and not result["unexplained_divergences"]


@pytest.mark.parametrize(
    "verdict", ["repair", "reject", "obsolete", "duplicate", "superseded", "delivered"]
)
def test_revoked_approval_is_a_named_source_difference(host: dict[str, Any], verdict: str) -> None:
    review(host, verdict=verdict)
    result = compare(host)
    assert result["legacy"]["selected"]
    assert result["rotisserie"]["status"] == "review_hold"
    assert (
        result["deliberate_holds"][0]["reason"]
        == "latest_semantic_verdict_revoked_earlier_approval"
    )
    assert not result["unexplained_divergences"]


@pytest.mark.parametrize(
    "state", ["FAILURE", "PENDING", "UNKNOWN", "", "CANCELLED", "SKIPPED", "NEUTRAL"]
)
def test_source_required_check_interpreter_is_executed(host: dict[str, Any], state: str) -> None:
    host["checks"][0]["state"] = state
    result = compare(host)
    assert result["matches"]
    assert result["legacy"]["selected"] == (result["rotisserie"]["status"] == "selected")
    assert "checks" in result["legacy"]["reads"]


@pytest.mark.parametrize(
    "rows", [None, [], [{"state": "SUCCESS"}], [{"name": "tests", "state": "SUCCESS"}] * 2]
)
def test_missing_or_ambiguous_contexts(host: dict[str, Any], rows: object) -> None:
    host["checks"] = rows
    result = compare(host)
    assert result["rotisserie"]["status"] in {"ci_hold", "readiness_hold"}
    if result["legacy"]["selected"]:
        assert result["deliberate_holds"]
    assert not result["unexplained_divergences"]


@pytest.mark.parametrize(
    "kind", ["stale_ci", "required_context_missing", "untrusted_checks", "incomplete_reviews"]
)
def test_complete_current_host_reads_are_required(host: dict[str, Any], kind: str) -> None:
    if kind == "stale_ci":
        host["checks_revision"] = "b" * 40
    elif kind == "required_context_missing":
        host["configuration"]["required_ci"].append("lint")
    elif kind == "untrusted_checks":
        host["attestations"]["checks"]["trusted"] = False
    else:
        host["attestations"]["host_reviews"]["complete"] = False
    result = compare(host)
    assert result["legacy"]["selected"]
    assert result["deliberate_holds"] and not result["unexplained_divergences"]


@pytest.mark.parametrize(
    "kind",
    [
        "draft",
        "conflict",
        "unknown",
        "closed",
        "changed_head",
        "changes_requested",
        "thread",
        "thread_pagination",
        "thread_comment_pagination",
    ],
)
def test_actual_mechanical_gates_hold_both_paths(host: dict[str, Any], kind: str) -> None:
    if kind == "draft":
        host["pr"]["isDraft"] = True
    elif kind == "conflict":
        host["pr"]["mergeable"] = "CONFLICTING"
    elif kind == "unknown":
        host["pr"]["mergeable"] = "UNKNOWN"
    elif kind == "closed":
        host["pr"]["state"] = "CLOSED"
    elif kind == "changed_head":
        host["expected_head"] = "b" * 40
    elif kind == "changes_requested":
        host["host_reviews"] = [{"state": "CHANGES_REQUESTED", "commit_id": "a" * 40}]
    else:
        host["inline_comments"] = [{"commit_id": "a" * 40}]
        host["threads"] = {
            "nodes": [
                {
                    "isResolved": False,
                    "comments": {
                        "nodes": [{"commit": {"oid": "a" * 40}}],
                        "pageInfo": {"hasNextPage": kind == "thread_comment_pagination"},
                    },
                }
            ],
            "pageInfo": {"hasNextPage": kind == "thread_pagination"},
        }
    result = compare(host)
    assert result["matches"] and not result["legacy"]["selected"]
    assert result["rotisserie"]["status"] != "selected"


@pytest.mark.parametrize(
    "kind",
    [
        "unknown_liveness",
        "missing_liveness",
        "untrusted_liveness",
        "stale_liveness",
        "busy_repairer",
        "capacity",
        "backpressure",
        "human",
        "prerequisite",
        "owner",
        "expired_lease",
        "duplicate",
        "incomplete_lineage",
        "unreachable_head",
        "ambiguous_identity",
        "incomplete_graph",
        "stale_graph",
        "future_graph",
        "stale_comments",
        "untrusted_comments",
        "unordered_comments",
    ],
)
def test_named_existing_controller_safety_holds(host: dict[str, Any], kind: str) -> None:
    if kind == "unknown_liveness":
        host["liveness"][0]["state"] = "unknown"
    elif kind == "missing_liveness":
        host["liveness"].pop()
    elif kind in {"untrusted_liveness", "stale_liveness"}:
        host["attestations"]["liveness"][
            "trusted" if kind == "untrusted_liveness" else "observed_at"
        ] = False if kind == "untrusted_liveness" else 9
    elif kind == "busy_repairer":
        host["liveness"][1]["state"] = "active"
    elif kind == "capacity":
        host["active"] = 4
    elif kind == "backpressure":
        host["configuration"]["backlog_limit"] = 4
        host["completion_backlog"] = 4
    elif kind == "human":
        host["issues"][0]["human_required"] = True
    elif kind == "prerequisite":
        host["issues"][0]["prerequisites"] = [13]
        issue = deepcopy(host["issues"][0])
        issue.update(number=13, prerequisites=[])
        host["issues"].append(issue)
    elif kind == "owner":
        host["pr"]["labels"].append("factory:6")
    elif kind == "expired_lease":
        host["leases"].append(
            {"id": "old", "work": 12, "worker": "6", "acquired_at": 1, "expires_at": 9}
        )
    elif kind == "duplicate":
        other = deepcopy(host["pr"])
        other["number"] = 16
        host["other_prs"].append(other)
    elif kind == "incomplete_lineage":
        host["attestations"]["lineage"]["complete"] = False
    elif kind == "unreachable_head":
        host["reachable_heads"].pop()
    elif kind == "ambiguous_identity":
        review(host, worker="77")
    elif kind == "incomplete_graph":
        host["attestations"]["graph"]["complete"] = False
    elif kind == "stale_graph":
        host["observed_at"] = 9
    elif kind == "future_graph":
        host["observed_at"] = 11
    elif kind in {"stale_comments", "untrusted_comments"}:
        host["attestations"]["comments"][
            "observed_at" if kind == "stale_comments" else "trusted"
        ] = 9 if kind == "stale_comments" else False
    else:
        host["comments"].reverse()
    result = compare(host)
    assert result["legacy"]["selected"]
    assert result["rotisserie"]["status"] != "selected"
    assert result["deliberate_holds"] and not result["unexplained_divergences"]


def test_untrusted_marker_and_old_approval_cannot_authorize(host: dict[str, Any]) -> None:
    host["comments"][-1]["performed_via_github_app"] = {"slug": "worker-app"}
    result = compare(host)
    assert result["matches"] and not result["legacy"]["selected"]
    host["comments"][-1].pop("performed_via_github_app")
    host["comments"][-1]["body"] = host["comments"][-1]["body"].replace("a" * 40, "b" * 40)
    result = compare(host)
    assert result["matches"] and not result["legacy"]["selected"]


def test_formal_approval_never_substitutes_for_worker_review(host: dict[str, Any]) -> None:
    host["comments"] = host["comments"][:2]
    host["host_reviews"] = [{"state": "APPROVED", "commit_id": "a" * 40}]
    result = compare(host)
    assert result["matches"] and not result["legacy"]["selected"]


def test_source_and_capture_integrity_are_checked_before_interpretation(
    host: dict[str, Any], tmp_path: Path
) -> None:
    raw = json.dumps(host).encode()
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="integrity"):
        CompletionCapture.read(raw + b" ", digest=digest, source_sha=SOURCE_SHA)
    with pytest.raises(ValueError, match="different source"):
        CompletionCapture.read(raw, digest=digest, source_sha="b" * 40)
    shutil.copytree(EXAMPLE / "source", tmp_path / "source")
    with pytest.raises(ValueError, match="repin"):
        verified_source(tmp_path / "source", "b" * 40)
    path = tmp_path / "source/factory-review-controller.py.txt.gz"
    path.write_bytes(gzip.compress(gzip.decompress(path.read_bytes()) + b"\n", mtime=0))
    with pytest.raises(ValueError, match="integrity"):
        verified_source(tmp_path / "source", SOURCE_SHA)


def test_no_source_transport_worker_or_mutation_can_run(
    host: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("comparison attempted external effect")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    source = SourceDecisions(verified_source(EXAMPLE / "source", SOURCE_SHA), host)
    for name in (
        "subprocess",
        "os",
        "sys",
        "time",
        "handle_review",
        "record_contribution",
        "main",
        "transition_pr_and_linked_issue",
        "reconcile_ci",
        "demote_ready",
    ):
        assert name not in source.env
    for args in (
        ["pr", "merge", "15"],
        ["workflow", "run", "worker.yml"],
        ["api", "-X", "PATCH", "repos/JoshCLWren/comic-pile/issues/12"],
    ):
        with pytest.raises(RuntimeError, match="unallowlisted"):
            source._checks(args)
        with pytest.raises(RuntimeError, match="unallowlisted"):
            source._json(args)
    compare(host)
    host["comments"] = host["comments"][:2]
    result = compare(host)
    assert result["legacy"]["suppressed_effects"]
    assert not result["remote_mutation"] and not result["worker_invoked"]


def test_unexplained_drift_is_not_excused_as_safety(
    host: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rotisserie.application.completion_controller import CompletionController

    monkeypatch.setattr(
        CompletionController, "tick", lambda *a, **k: CompletionResult(CompletionStatus.REVIEW)
    )
    result = compare(host)
    assert result["unexplained_divergences"] and not result["deliberate_holds"]
    host["checks"][0]["state"] = "FAILURE"
    monkeypatch.setattr(
        CompletionController, "tick", lambda *a, **k: CompletionResult(CompletionStatus.SELECTED)
    )
    result = compare(host)
    assert result["unexplained_divergences"][0]["reason"] == "rotisserie_selected_when_source_held"


def test_public_cli_never_initializes_graph_and_binds_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copytree(EXAMPLE, tmp_path, dirs_exist_ok=True)
    digest = hashlib.sha256((tmp_path / "capture.json").read_bytes()).hexdigest()
    args = [
        "--config",
        str(tmp_path / "config.toml"),
        "controller-completion-compare",
        "--capture",
        str(tmp_path / "capture.json"),
        "--capture-digest",
        digest,
        "--source-sha",
        SOURCE_SHA,
        "--source-directory",
        str(tmp_path / "source"),
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["evidence"]["capture_sha256"] == digest
    assert result["status"] == "match"
    assert not (tmp_path / "unused.json").exists()
    assert not (tmp_path / ".rotisserie-state/graph.json").exists()
    (tmp_path / "capture.json").write_text("{}")
    assert main(args) == 2
    assert "integrity" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize(
    "kind", ["failed", "unavailable", "zero", "non_product_failure", "stale_failure", "incomplete"]
)
def test_actual_main_health_source_precedes_ready_drain(host: dict[str, Any], kind: str) -> None:
    if kind == "failed":
        host["main_runs"]["workflow_runs"][0]["conclusion"] = "failure"
    elif kind == "unavailable":
        host["main_runs"] = None
    elif kind == "zero":
        host["main_runs"]["workflow_runs"] = []
    elif kind == "non_product_failure":
        host["main_runs"]["workflow_runs"][0].update(
            name="Factory Dispatcher", conclusion="failure"
        )
    elif kind == "stale_failure":
        host["main_runs"]["workflow_runs"][0].update(head_sha="b" * 40, conclusion="failure")
    else:
        host["attestations"]["main_health"]["complete"] = False
    result = compare(host)
    if kind in {"failed", "unavailable"}:
        assert not result["legacy"]["selected"] and result["matches"]
        assert result["legacy"]["reads"] == ["main_health"]
    elif kind == "incomplete":
        assert result["legacy"]["selected"] and result["deliberate_holds"]
    else:
        assert result["matches"] and result["legacy"]["selected"]


def test_semantic_marker_in_prose_is_a_provenance_hold(host: dict[str, Any]) -> None:
    host["comments"][-1]["body"] += "\nReview prose containing an embedded approval marker"
    result = compare(host)
    assert result["legacy"]["selected"]
    assert result["rotisserie"]["status"] == "provenance_hold"
    assert result["deliberate_holds"]


@pytest.mark.parametrize(
    "phase", ["claimed", "attempting", "unknown", "completed", "refused", "failed"]
)
def test_durable_completion_receipts_suppress_every_fresh_attempt(
    host: dict[str, Any], phase: str
) -> None:
    initial = compare(host)
    binding = initial["rotisserie"]["binding"]
    assert binding is not None
    host["completion_records"] = [{"schema_version": 1, "binding": binding, "phase": phase}]
    result = compare(host)
    assert result["legacy"]["selected"]
    assert result["rotisserie"]["status"] == "ownership_hold"
    assert result["deliberate_holds"]


def test_checked_golden_is_generated_from_exact_capture_bytes() -> None:
    raw = (EXAMPLE / "capture.json").read_bytes()
    capture = CompletionCapture.read(
        raw, digest=hashlib.sha256(raw).hexdigest(), source_sha=SOURCE_SHA
    )
    result = compare_completion_capture(
        capture, source_directory=EXAMPLE / "source", source_sha=SOURCE_SHA
    )
    assert result == json.loads((EXAMPLE / "comparison.json").read_bytes())


@pytest.mark.parametrize(
    "status",
    [
        CompletionStatus.CAPACITY,
        CompletionStatus.BACKPRESSURE,
        CompletionStatus.HUMAN,
        CompletionStatus.PREREQUISITE,
        CompletionStatus.OWNERSHIP,
        CompletionStatus.LIVENESS,
        CompletionStatus.ACQUISITION,
        CompletionStatus.PROVENANCE,
        CompletionStatus.CI,
        CompletionStatus.READINESS,
        CompletionStatus.REVIEW,
        CompletionStatus.ZERO,
        CompletionStatus.STALE,
    ],
)
def test_hold_names_alone_cannot_excuse_drift(
    host: dict[str, Any], monkeypatch: pytest.MonkeyPatch, status: CompletionStatus
) -> None:
    from rotisserie.application.completion_controller import CompletionController

    monkeypatch.setattr(CompletionController, "tick", lambda *a, **k: CompletionResult(status))
    result = compare(host)
    assert result["unexplained_divergences"] and not result["deliberate_holds"]


@pytest.mark.parametrize(
    "kind",
    [
        "foreign_base",
        "foreign_config",
        "ambiguous_workers",
        "boolean_attestation",
        "wrong_main_sha",
    ],
)
def test_scope_and_malformed_capture_fail_closed(host: dict[str, Any], kind: str) -> None:
    if kind == "foreign_base":
        host["pr"]["base_repository"] = "other/repo"
    elif kind == "foreign_config":
        host["schema_version"] = 2
    elif kind == "ambiguous_workers":
        host["configuration"]["workers"].append("6")
    elif kind == "boolean_attestation":
        host["attestations"]["lineage"]["complete"] = "true"
    else:
        host["main_sha"] = "b" * 40
    with pytest.raises(ValueError):
        compare(host)


@pytest.mark.parametrize(
    "kind", ["unknown_owner", "closed_work", "conflicting_owner_labels", "ambiguous_host_review"]
)
def test_product_and_identity_translation_holds(host: dict[str, Any], kind: str) -> None:
    if kind == "unknown_owner":
        host["pr"]["labels"].append("factory:77")
    elif kind == "closed_work":
        host["issues"][0]["state"] = "closed"
    elif kind == "conflicting_owner_labels":
        host["pr"]["labels"].extend(["factory:6", "factory:7"])
    else:
        host["host_reviews"] = [{"state": "UNRECOGNIZED", "commit_id": "a" * 40}]
    result = compare(host)
    assert result["legacy"]["selected"] and result["deliberate_holds"]
    assert not result["unexplained_divergences"]


def test_pinned_drain_order_keeps_expected_head_at_merge_boundary() -> None:
    workflow = verified_source(EXAMPLE / "source", SOURCE_SHA)[
        "factory-ready-merge-drain.yml.txt"
    ].decode()
    authorization = workflow.index('authorization="$(python3 "$review_controller" authorized')
    expected_head_check = workflow.index(".head // empty", authorization)
    mechanical = workflow.index('gate="$(python3 "$review_controller" gates', expected_head_check)
    merge = workflow.index('gh pr merge "$pr"', mechanical)
    expected_head_merge = workflow.index('--match-head-commit "$expected_head"', merge)
    post_merge = workflow.index('complete_post_merge_closure "$pr"', expected_head_merge)
    assert (
        authorization < expected_head_check < mechanical < merge < expected_head_merge < post_merge
    )
