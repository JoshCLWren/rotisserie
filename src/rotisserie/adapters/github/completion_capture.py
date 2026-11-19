"""One bounded captured host view -> source decisions and public completion inputs.

No acquisition credentials or host clients exist here. Capture attestations are
fixture assertions, not production authority. Configuration owns product names.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from rotisserie.adapters.github.completion_source import (
    SOURCE_FILES,
    SourceDecisions,
    verified_source,
)
from rotisserie.adapters.github.review_evidence import (
    GitHubReviewEvidence,
    ReviewEvidenceError,
    TrustedCommentPolicy,
)
from rotisserie.application.completion_controller import (
    CompletionController,
    CompletionEffect,
    CompletionPhase,
    CompletionResult,
    CompletionStatus,
    CompletionView,
)
from rotisserie.application.controller import AcquisitionFailure
from rotisserie.application.process import (
    _integer,
    _object,
    _repository,
    _secret_free,
    _text,
    _version,
)
from rotisserie.domain import (
    Boundary,
    BoundaryId,
    BoundaryKind,
    Change,
    ChangeId,
    CheckStatus,
    GraphSnapshot,
    Lease,
    LeaseId,
    ReviewIndependenceSignals,
    Revision,
    RevisionId,
    Work,
    Worker,
    WorkerId,
    WorkId,
    WorkState,
)
from rotisserie.operator.completion import completion_request_from_dict, completion_view_from_dict

ATTESTATIONS = frozenset(
    {
        "main_health",
        "graph",
        "comments",
        "lineage",
        "checks",
        "host_reviews",
        "inline_comments",
        "threads",
        "liveness",
    }
)


def _array(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("captured host rows require arrays")
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("capture attestations require booleans")
    return value


def _sha(value: object) -> str:
    text = _text(value)
    if not re.fullmatch(r"[0-9a-f]{40}", text):
        raise ValueError("capture requires exact SHA")
    return text


@dataclass(frozen=True)
class CompletionCapture:
    raw: dict[str, Any]
    digest: str

    @classmethod
    def read(cls, data: bytes, *, digest: str, source_sha: str) -> CompletionCapture:
        if len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("capture integrity mismatch or oversized capture")
        raw = _object(
            json.loads(data),
            {
                "schema_version",
                "main_sha",
                "main_runs",
                "repository",
                "source_sha",
                "selected_pr",
                "expected_head",
                "observed_at",
                "pr",
                "other_prs",
                "issues",
                "comments",
                "checks",
                "checks_revision",
                "host_reviews",
                "inline_comments",
                "threads",
                "attestations",
                "reachable_heads",
                "liveness",
                "leases",
                "completion_records",
                "active",
                "completion_backlog",
                "configuration",
            },
        )
        _secret_free(raw)
        _version(raw)
        if _sha(raw["source_sha"]) != source_sha:
            raise ValueError("capture belongs to a different source SHA")
        _repository(raw["repository"])
        if _sha(raw["main_sha"]) != source_sha:
            raise ValueError("main-health view belongs to a different source checkout")
        _sha(raw["expected_head"])
        _sha(raw["checks_revision"])
        for key in ("selected_pr", "observed_at", "active", "completion_backlog"):
            _integer(raw[key])
        attestations = _object(raw["attestations"], set(ATTESTATIONS))
        for value in attestations.values():
            item = _object(value, {"complete", "trusted", "observed_at"})
            _bool(item["complete"])
            _bool(item["trusted"])
            _integer(item["observed_at"])
        for key in (
            "other_prs",
            "issues",
            "reachable_heads",
            "liveness",
            "leases",
            "completion_records",
        ):
            _array(raw[key])
        for head in raw["reachable_heads"]:
            _sha(head)
        if len(set(raw["reachable_heads"])) != len(raw["reachable_heads"]):
            raise ValueError("ambiguous reachable head capture")
        for key in ("comments", "checks", "host_reviews", "inline_comments"):
            if raw[key] is not None:
                _array(raw[key])
        return cls(raw, digest)


class _PlanningPort:
    def __init__(self, view: CompletionView, complete: bool) -> None:
        self.view, self.acquisition_complete = view, complete

    def acquire(self) -> CompletionView:
        if not self.acquisition_complete:
            raise AcquisitionFailure("captured graph acquisition is incomplete")
        return self.view

    def claim(self, effect: CompletionEffect) -> None:
        raise AssertionError("comparison invoked claim")

    def complete(self, effect: CompletionEffect) -> CompletionPhase:
        raise AssertionError("comparison invoked merge")


def compare_completion_capture(
    capture: CompletionCapture, *, source_directory: Path, source_sha: str
) -> dict[str, Any]:
    """Read-only version-1 comparison. It cannot be used as a cutover ShadowReport."""
    files = verified_source(source_directory, source_sha)
    c = capture.raw
    repository = _repository(c["repository"])
    cfg = _object(
        c["configuration"],
        {
            "marker_namespace",
            "worker_namespace",
            "bot_login",
            "trusted_associations",
            "trusted_app_slugs",
            "workers",
            "completion_owner",
            "ready_label",
            "owner_labels",
            "owner_label_pattern",
            "at",
            "max_observation_age",
            "lease_ttl",
            "wip_limit",
            "backlog_limit",
            "required_ci",
        },
    )
    namespace = _text(cfg["worker_namespace"])
    tokens = [_text(x) for x in _array(cfg["workers"])]
    if len(set(tokens)) != len(tokens) or any(x != x.casefold() or x == "unknown" for x in tokens):
        raise ValueError("ambiguous configured worker identities")

    def worker(token: object) -> WorkerId:
        return WorkerId(namespace, _text(token))

    def worker_dict(token: str) -> dict[str, str]:
        w = worker(token)
        return {"namespace": w.namespace, "value": w.value}

    at, max_age = _integer(cfg["at"]), _integer(cfg["max_observation_age"])
    failures: list[str] = []

    def attested(key: str) -> bool:
        a = c["attestations"][key]
        valid = a["complete"] and a["trusted"] and 0 <= at - a["observed_at"] <= max_age
        if not valid:
            failures.append(f"{key}_read_not_complete_trusted_fresh")
        return bool(valid)

    attest = {key: attested(key) for key in sorted(ATTESTATIONS)}
    pr = _object(
        c["pr"],
        {
            "number",
            "work",
            "base_repository",
            "headRefOid",
            "headRefName",
            "body",
            "labels",
            "state",
            "isDraft",
            "mergeable",
        },
    )
    _bool(pr["isDraft"])
    _array(pr["labels"])
    number = _integer(pr["number"])
    if number != c["selected_pr"] or number <= 0:
        raise ValueError("capture selected PR mismatch")
    repo_name = f"{repository.owner}/{repository.name}"
    for item in [pr, *c["other_prs"]]:
        if item.get("base_repository") != repo_name:
            raise ValueError("captured PR escaped configured repository")
    head, expected = _sha(pr["headRefOid"]), _sha(c["expected_head"])
    source = SourceDecisions(files, c)
    baseline = source.decide()
    mergeable, host_review = source.gates()
    declared = source.env["producer_worker_from_pr"](branch=pr["headRefName"], body=pr["body"])
    collector = GitHubReviewEvidence(
        repository,
        namespace=_text(cfg["marker_namespace"]),
        worker_namespace=namespace,
        policy=TrustedCommentPolicy(
            _text(cfg["bot_login"]),
            frozenset(_text(x) for x in _array(cfg["trusted_associations"])),
            frozenset(_text(x) for x in _array(cfg["trusted_app_slugs"])),
        ),
    )
    # Latest semantic verdict per worker, exact head only; retain every lineage record.
    # Source current_head_approvers deliberately sees the original unfiltered capture.
    marker = re.compile(
        rf"^<!-- {re.escape(collector.namespace)}-semantic-review-v1:pr-(\d+):"
        r"head-([0-9a-f]{40}):reviewer-([^:\s>]+):producer-([^:\s>]+):"
        r"verdict-(approve|repair|reject|obsolete|duplicate|superseded|delivered) -->$"
    )
    comments = c["comments"] or []
    ids: list[Any] = [item.get("id") for item in comments if isinstance(item, dict)]
    comments_ordered = (
        all(type(x) is int for x in ids) and len(ids) == len(comments) and ids == sorted(set(ids))
    )
    if not comments_ordered:
        failures.append("semantic_comment_order_ambiguous")
    latest: dict[str, dict[str, Any]] = {}
    lineage_comments = []
    recorded_heads = set()
    revoked = False
    for item in comments:
        if not isinstance(item, dict):
            failures.append("comment_row_ambiguous")
            continue
        bodies = collector.trusted_bodies([item])
        if not bodies:
            continue
        body = bodies[0].strip()
        parsed = collector.parse_head_contributor_marker(body)
        if parsed and parsed.pr == number:
            lineage_comments.append(item)
            recorded_heads.add(parsed.head)
        parsed_review = marker.fullmatch(body)
        if parsed_review and int(parsed_review[1]) == number and parsed_review[2] == head:
            identity = parsed_review[3].casefold()
            if identity in latest and parsed_review[5] != "approve":
                revoked = True
            latest[identity] = item
    lineage_complete = (
        attest["lineage"]
        and head in c["reachable_heads"]
        and recorded_heads <= set(c["reachable_heads"])
    )
    if not lineage_complete:
        failures.append("reachable_contributor_lineage_incomplete")
    try:
        all_signals = collector.collect(comments, pr=number, head=head, producer=declared)
        latest_signals = collector.collect(
            [*lineage_comments, *latest.values()], pr=number, head=head, producer=declared
        )
        signals = replace(all_signals, approvers=latest_signals.approvers)
        signals = replace(
            signals, contributors=signals.contributors | ({worker(declared)} if declared else set())
        )
        identity_complete = signals.contributors | signals.approvers <= {worker(x) for x in tokens}
    except ReviewEvidenceError:
        signals = ReviewIndependenceSignals(RevisionId(repository, head))
        identity_complete = False
        failures.append("producer_attestation_ambiguous")
    if not identity_complete:
        failures.append("worker_identity_unresolved")

    works = []
    boundaries = []
    edges: set[tuple[WorkId, WorkId]] = set()
    for value in c["issues"]:
        issue = _object(
            value, {"number", "title", "state", "labels", "prerequisites", "human_required"}
        )
        identity = WorkId(repository, str(_integer(issue["number"])))
        state = {"open": WorkState.OPEN, "closed": WorkState.COMPLETED}.get(issue["state"])
        if state is None:
            raise ValueError("unknown captured issue state")
        works.append(Work(identity, _text(issue["title"]), state))
        if _bool(issue["human_required"]):
            # Explicit human-only work cannot be silently approved by a fixture boolean.
            boundaries.append(
                Boundary(BoundaryId(f"human-{identity.key}"), identity, BoundaryKind.HUMAN_APPROVAL)
            )
        edges.update(
            (identity, WorkId(repository, str(_integer(x)))) for x in _array(issue["prerequisites"])
        )
    changes = tuple(
        Change(
            ChangeId(repository, str(item["number"])),
            WorkId(repository, str(item["work"])),
            RevisionId(repository, _sha(item["headRefOid"])),
            worker(declared) if item is pr and declared else None,
        )
        for item in [pr, *c["other_prs"]]
    )
    revisions = tuple(Revision(x) for x in sorted({x.head for x in changes} | {signals.revision}))
    leases = tuple(
        Lease(
            LeaseId(_text(item["id"])),
            WorkId(repository, str(_integer(item["work"]))),
            worker(item["worker"]),
            _integer(item["acquired_at"]),
            _integer(item["expires_at"]),
        )
        for item in c["leases"]
    )
    labels = {item["name"] if isinstance(item, dict) else item for item in pr["labels"]}
    owner_labels = cfg["owner_labels"]
    if not isinstance(owner_labels, dict) or any(x not in tokens for x in owner_labels.values()):
        raise ValueError("invalid owner label configuration")
    owner_pattern = re.compile(_text(cfg["owner_label_pattern"]))
    if any(owner_pattern.fullmatch(label) and label not in owner_labels for label in labels):
        attest["graph"] = False
        failures.append("graph_read_not_complete_trusted_fresh")
        failures.append("owner_identity_unresolved")
    for label in labels:
        if label in owner_labels and not any(x.work == changes[0].work for x in leases):
            # Owner labels are a fence even without a resolvable full durable lease.
            leases += (
                Lease(
                    LeaseId(f"captured-owner-{number}"),
                    changes[0].work,
                    worker(owner_labels[label]),
                    at,
                    at + 1,
                ),
            )
    snapshot = GraphSnapshot(
        works=tuple(works),
        changes=changes,
        revisions=revisions,
        workers=tuple(Worker(worker(x)) for x in tokens),
        leases=leases,
        boundaries=tuple(boundaries),
        dependencies=frozenset(edges),
    )
    checks = []
    checks_identity_complete = True
    seen = set()
    for row in c["checks"] or []:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or row["name"] in seen:
            checks_identity_complete = False
            continue
        seen.add(row["name"])
        check_state = str(row.get("state") or "").upper()
        status = (
            CheckStatus.PASSED
            if check_state in source.env["PASSING_CHECK_STATES"]
            else CheckStatus.FAILED
            if check_state in source.env["FAILING_CHECK_STATES"]
            else CheckStatus.PENDING
        )
        checks.append(
            {
                "revision": c["checks_revision"],
                "name": row["name"],
                "status": status,
                "required": True,
            }
        )
    if not checks_identity_complete:
        failures.append("required_context_identity_ambiguous")
    if set(seen) != set(cfg["required_ci"]):
        failures.append("required_context_set_mismatch")
    host_rows_complete = all(
        isinstance(row, dict)
        and row.get("state")
        in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}
        and isinstance(row.get("commit_id"), str)
        for row in c["host_reviews"] or []
    ) and all(isinstance(row, dict) for row in c["inline_comments"] or [])
    if not host_rows_complete:
        failures.append("host_review_payload_ambiguous")
    evidence_complete = (
        all(
            attest[key]
            for key in ("main_health", "checks", "host_reviews", "inline_comments", "threads")
        )
        and checks_identity_complete
        and host_rows_complete
    )
    reviews = []
    for token, item in sorted(latest.items()):
        parsed_review = marker.fullmatch(item["body"].strip())
        assert parsed_review
        verdict = parsed_review[5]
        reviews.append(
            {
                "id": f"comment-{item['id']}",
                "revision": head,
                "reviewer": worker_dict(token),
                "decision": "approved" if verdict == "approve" else "changes_requested",
                "required": True,
            }
        )
    readiness = [
        {
            "revision": head,
            "name": name,
            "status": "passed" if passed else "failed",
            "required": True,
        }
        for name, passed in (
            (
                "main_health",
                baseline.get("main_health") is not None and baseline["main_health"]["failing"] == 0,
            ),
            ("mergeable", mergeable["decision"] == "pass"),
            ("host_review", host_review["decision"] == "pass"),
            ("eligibility", pr["state"] == "OPEN" and cfg["ready_label"] in labels),
        )
    ]
    live_rows = []
    for row in c["liveness"]:
        item = _object(row, {"worker", "state"})
        live_rows.append(
            {
                **worker_dict(item["worker"]),
                "state": item["state"] if attest["liveness"] else "unknown",
            }
        )
    envelope = {
        "schema_version": 1,
        "controller": {
            "schema_version": 1,
            "version": f"{source_sha}:{capture.digest}",
            "snapshot": snapshot.to_dict(),
            "observed_at": c["observed_at"],
            "active": c["active"],
            "completion_backlog": c["completion_backlog"],
            "liveness": live_rows,
        },
        "candidates": [
            {
                "provenance": {
                    "change": str(number),
                    "revision": head,
                    "contributors": [worker_dict(x.value) for x in sorted(signals.contributors)],
                    "approvers": [worker_dict(x.value) for x in sorted(signals.approvers)],
                    "producer": worker_dict(signals.producer.value) if signals.producer else None,
                    "producer_attested": signals.producer_attested,
                    "provenance_recorded": signals.provenance_recorded,
                    "observed_at": c["attestations"]["comments"]["observed_at"],
                    "complete": attest["comments"]
                    and lineage_complete
                    and identity_complete
                    and comments_ordered,
                    "trusted": c["attestations"]["comments"]["trusted"],
                },
                "reviews": reviews,
                "ci": checks,
                "readiness": readiness,
                "observed_at": c["observed_at"],
                "complete": evidence_complete,
                "trusted": evidence_complete,
            }
        ],
        "liveness_complete": attest["liveness"],
        "records": c["completion_records"],
    }
    request_raw = {
        "schema_version": 1,
        "repository": c["repository"],
        "change": str(number),
        "revision": expected,
        "worker": worker_dict(_text(cfg["completion_owner"])),
        "lease_id": f"comparison-{capture.digest}",
        "operation": f"comparison-{capture.digest}",
        "at": at,
        "max_observation_age": max_age,
        "lease_ttl": cfg["lease_ttl"],
        "workers": [worker_dict(x) for x in tokens],
        "wip_limit": cfg["wip_limit"],
        "backlog_limit": cfg["backlog_limit"],
        "reserved_review": 0,
        "required_ci": cfg["required_ci"],
        "required_readiness": ["main_health", "mergeable", "host_review", "eligibility"],
    }
    view, request = completion_view_from_dict(envelope), completion_request_from_dict(request_raw)
    result = CompletionController(_PlanningPort(view, attest["graph"])).tick(request)
    selected = result.status is CompletionStatus.SELECTED
    differences: list[dict[str, object]] = []
    if baseline["selected"] != selected:
        category, reason = (
            ("unexplained_divergence", "rotisserie_selected_when_source_held")
            if selected
            else _explain(result, view, request_raw, failures, revoked)
        )
        differences.append(
            {
                "category": category,
                "reason": reason,
                "source_selected": baseline["selected"],
                "rotisserie_status": result.status,
            }
        )
    return {
        "schema_version": 1,
        "kind": "completion_source_comparison",
        "source_sha": source_sha,
        "source_files": SOURCE_FILES,
        "capture_sha256": capture.digest,
        "binding_sha256": hashlib.sha256(
            json.dumps(
                {
                    "source": SOURCE_FILES,
                    "source_sha": source_sha,
                    "capture_sha256": capture.digest,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "legacy": baseline,
        "completion_view": envelope,
        "completion_request": request_raw,
        "rotisserie": result.to_dict(),
        "matches": not differences,
        "comparison_scope": "one_declared_exact_head_completion_eligibility",
        "shared_hold": not baseline["selected"] and not selected,
        "safety_holds": [
            {
                "category": "deliberate_safety_hold",
                "reason": reason,
                "rotisserie_status": result.status,
            }
            for category, reason in [_explain(result, view, request_raw, failures, revoked)]
            if not selected and category == "deliberate_safety_hold"
        ],
        "deliberate_holds": [x for x in differences if x["category"] == "deliberate_safety_hold"],
        "unexplained_divergences": [
            x for x in differences if x["category"] == "unexplained_divergence"
        ],
        "acquisition_findings": sorted(set(failures)),
        "remote_mutation": False,
        "worker_invoked": False,
        "mutation_boundary": {
            "bounded": "one_declared_completion_target",
            "cas": "required_by_completion_controller_not_enforced_by_comparison",
            "expected_head": "planned_only_no_host_merge_transaction",
            "provenance": "known_attested_producer_and_complete_reachable_contributors",
            "uncertainty": "all_durable_completion_receipts_fence_fresh_attempts",
        },
        "source_classification": {
            "factory_completion_controller.py": "adopter_demand_capacity_and_worker_routing",
            "factory_full_completion_controller.py": "adopter_demand_and_dispatch_signalling",
            "factory-work-controller.py": "adopter_ordering_acquisition_and_ownership",
            "factory_post_merge_closure.py": (
                "separate_post_merge_issue_and_acceptance_reconciliation"
            ),
            "factory-release-reconcile.yml": "separate_release_dispatch_no_completion_authority",
            "factory_migration_lane.py": "separate_migration_reconciliation_not_executed",
        },
        "production_authority": False,
        "cutover_authority": False,
        "excluded_effects": [
            "claim",
            "expected_head_merge",
            "repair_or_review_loop",
            "post_merge_linked_issue_closure",
            "acceptance_parent_checks",
            "release_publication",
            "reconciliation",
        ],
    }


def _explain(
    result: CompletionResult,
    view: CompletionView,
    request: dict[str, Any],
    failures: list[str],
    revoked: bool,
) -> tuple[str, str]:
    status, e = result.status, view.candidates[0]
    c, snapshot = view.controller, view.controller.graph.snapshot
    signals = e.provenance.signals
    work = snapshot.changes[0].work
    owner = WorkerId(request["worker"]["namespace"], request["worker"]["value"])
    known = {x.id for x in snapshot.workers}
    age_invalid = not 0 <= request["at"] - c.observed_at <= request["max_observation_age"]
    reasons = {
        CompletionStatus.ACQUISITION: (
            age_invalid or "graph_read_not_complete_trusted_fresh" in failures,
            "complete_fresh_repository_view_required",
        ),
        CompletionStatus.HUMAN: (
            not snapshot.boundaries_satisfied(work),
            "explicit_human_gate_required",
        ),
        CompletionStatus.PREREQUISITE: (
            any(
                x.state is not WorkState.COMPLETED and x.id in snapshot.prerequisites(work)
                for x in snapshot.works
            ),
            "explicit_prerequisite_required",
        ),
        CompletionStatus.OWNERSHIP: (
            any(x.work == work or x.worker == owner for x in snapshot.leases)
            or any(x.binding.lease.work == work for x in view.records)
            or sum(x.work == work for x in snapshot.changes) != 1,
            "bounded_durable_ownership_and_duplicate_fence",
        ),
        CompletionStatus.LIVENESS: (
            not view.liveness_complete
            or any(c.state(x).value == "unknown" for x in known)
            or any(
                c.state(x).value != "idle"
                for x in {owner} | signals.contributors | signals.approvers
            ),
            "complete_fresh_trusted_idle_participants_required",
        ),
        CompletionStatus.CAPACITY: (
            c.active >= request["wip_limit"],
            "explicit_completion_capacity_required",
        ),
        CompletionStatus.BACKPRESSURE: (
            request["backlog_limit"] is not None
            and c.completion_backlog >= request["backlog_limit"],
            "configured_completion_backpressure_required",
        ),
    }
    if status in reasons and reasons[status][0]:
        return "deliberate_safety_hold", reasons[status][1]
    if status is CompletionStatus.PROVENANCE and (
        not e.provenance.complete
        or not e.provenance.trusted
        or not 0 <= request["at"] - e.provenance.observed_at <= request["max_observation_age"]
        or not e.provenance.signals.producer_attested
        or not e.provenance.signals.provenance_recorded
    ):
        return (
            "deliberate_safety_hold",
            "complete_attested_exact_head_producer_and_lineage_required",
        )
    if status is CompletionStatus.REVIEW and revoked:
        return "deliberate_safety_hold", "latest_semantic_verdict_revoked_earlier_approval"
    if status is CompletionStatus.CI and (
        any(x.id.revision.value != request["revision"] for x in e.ci)
        or "required_context_set_mismatch" in failures
    ):
        return "deliberate_safety_hold", "exact_head_complete_required_context_set_required"
    if status is CompletionStatus.READINESS:
        if not e.complete:
            return "deliberate_safety_hold", "complete_trusted_mechanical_reads_required"
        if any(x.id == work and x.state is not WorkState.OPEN for x in snapshot.works):
            return "deliberate_safety_hold", "configured_linked_work_open_required"
    return (
        "unexplained_divergence",
        "source_and_controller_decisions_differ_without_a_proven_safety_reason",
    )
