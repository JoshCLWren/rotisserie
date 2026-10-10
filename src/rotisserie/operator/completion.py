"""Strict version-1 captured completion contracts; fixtures grant no live authority."""

from __future__ import annotations

from rotisserie.application.completion_controller import (
    CompletionBinding,
    CompletionEvidence,
    CompletionPhase,
    CompletionRecord,
    CompletionRequest,
    CompletionView,
)
from rotisserie.application.process import (
    _integer,
    _object,
    _repository,
    _secret_free,
    _text,
    _version,
    _worker,
)
from rotisserie.domain import (
    ChangeId,
    Check,
    CheckId,
    CheckStatus,
    Lease,
    LeaseId,
    Review,
    ReviewDecision,
    ReviewId,
    RevisionId,
    SchedulingPolicy,
    WorkId,
)
from rotisserie.operator.controller import controller_view_from_dict, review_view_from_dict


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("completion contract requires arrays")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("completion attestation requires booleans")
    return value


def _names(value: object) -> frozenset[str]:
    names = tuple(_text(x) for x in _array(value))
    if len(set(names)) != len(names):
        raise ValueError("ambiguous configured completion gates")
    return frozenset(names)


def completion_request_from_dict(value: object) -> CompletionRequest:
    _secret_free(value)
    r = _object(
        value,
        {
            "schema_version",
            "repository",
            "change",
            "revision",
            "worker",
            "lease_id",
            "operation",
            "at",
            "max_observation_age",
            "lease_ttl",
            "workers",
            "wip_limit",
            "backlog_limit",
            "reserved_review",
            "required_ci",
            "required_readiness",
        },
    )
    _version(r)
    repository = _repository(r["repository"])
    workers = tuple(_worker(x) for x in _array(r["workers"]))
    if len(set(workers)) != len(workers):
        raise ValueError("ambiguous completion worker inventory")
    return CompletionRequest(
        repository,
        ChangeId(repository, _text(r["change"])),
        RevisionId(repository, _text(r["revision"])),
        _worker(r["worker"]),
        LeaseId(_text(r["lease_id"])),
        _text(r["operation"]),
        SchedulingPolicy(
            _integer(r["wip_limit"]),
            _integer(r["reserved_review"]),
            _integer(r["backlog_limit"]) if r["backlog_limit"] is not None else None,
        ),
        _integer(r["at"]),
        _integer(r["max_observation_age"]),
        _integer(r["lease_ttl"]),
        frozenset(workers),
        _names(r["required_ci"]),
        _names(r["required_readiness"]),
    )


def completion_view_from_dict(value: object) -> CompletionView:
    _secret_free(value)
    raw = _object(
        value, {"schema_version", "controller", "candidates", "liveness_complete", "records"}
    )
    _version(raw)
    controller = controller_view_from_dict(raw["controller"])

    def evidence(value: object) -> CompletionEvidence:
        e = _object(
            value,
            {"provenance", "reviews", "ci", "readiness", "observed_at", "complete", "trusted"},
        )
        # Reuse the established strict exact-head provenance acquisition contract.
        provenance = review_view_from_dict(
            {
                "schema_version": 1,
                "controller": raw["controller"],
                "candidates": [e["provenance"]],
                "liveness_complete": raw["liveness_complete"],
                "bindings": [],
            }
        ).candidates[0]
        repository = provenance.change.repository
        reviews = []
        for value in _array(e["reviews"]):
            x = _object(value, {"id", "revision", "reviewer", "decision", "required"})
            reviews.append(
                Review(
                    ReviewId(_text(x["id"])),
                    RevisionId(repository, _text(x["revision"])),
                    _worker(x["reviewer"]),
                    ReviewDecision(_text(x["decision"])),
                    _boolean(x["required"]),
                )
            )

        def checks(value: object) -> tuple[Check, ...]:
            result = []
            for item in _array(value):
                x = _object(item, {"revision", "name", "status", "required"})
                result.append(
                    Check(
                        CheckId(RevisionId(repository, _text(x["revision"])), _text(x["name"])),
                        CheckStatus(_text(x["status"])),
                        _boolean(x["required"]),
                    )
                )
            return tuple(result)

        return CompletionEvidence(
            provenance,
            tuple(reviews),
            checks(e["ci"]),
            checks(e["readiness"]),
            _integer(e["observed_at"]),
            _boolean(e["complete"]),
            _boolean(e["trusted"]),
        )

    records = []
    for value in _array(raw["records"]):
        x = _object(value, {"schema_version", "binding", "phase"})
        _version(x)
        b = _object(
            x["binding"], {"schema_version", "lease", "evidence", "request", "authorization_digest"}
        )
        _version(b)
        lease = _object(b["lease"], {"id", "work", "worker", "acquired_at", "expires_at"})
        work = _object(lease["work"], {"repository", "key"})
        records.append(
            CompletionRecord(
                CompletionBinding(
                    Lease(
                        LeaseId(_text(lease["id"])),
                        WorkId(_repository(work["repository"]), _text(work["key"])),
                        _worker(lease["worker"]),
                        _integer(lease["acquired_at"]),
                        _integer(lease["expires_at"]),
                    ),
                    evidence(b["evidence"]),
                    completion_request_from_dict(b["request"]),
                    _text(b["authorization_digest"]),
                ),
                CompletionPhase(_text(x["phase"])),
            )
        )
    return CompletionView(
        controller,
        tuple(evidence(x) for x in _array(raw["candidates"])),
        _boolean(raw["liveness_complete"]),
        tuple(records),
    )
