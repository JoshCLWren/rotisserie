"""Versioned, provider-neutral wire contract for a worker process bridge."""

from __future__ import annotations

from typing import Any

from rotisserie.application.runtime import (
    Assignment,
    AssignmentId,
    AttemptId,
    Authority,
    AuthorityManifest,
    EvidenceType,
    ExecutionResult,
    Outcome,
    ResumePacket,
    RuntimeEvidence,
    require_secret_free,
)
from rotisserie.domain import ChangeId, RepositoryId, RevisionId, WorkerId, WorkId


def _secret_free(value: object) -> None:
    if isinstance(value, str):
        require_secret_free(value, "process contract")
    elif isinstance(value, dict):
        for key, item in value.items():
            _secret_free(key)
            _secret_free(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _secret_free(item)


def _object(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid process contract fields")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("process contract requires a string")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("process contract requires an integer")
    return value


def _version(raw: dict[str, Any]) -> None:
    if _integer(raw["schema_version"]) != 1:
        raise ValueError("unsupported process contract version")


def _repository(value: object) -> RepositoryId:
    raw = _object(value, {"host", "owner", "name"})
    return RepositoryId(*(_text(raw[key]) for key in ("host", "owner", "name")))


def _worker(value: object) -> WorkerId:
    raw = _object(value, {"namespace", "value"})
    return WorkerId(_text(raw["namespace"]), _text(raw["value"]))


def _revision(value: object) -> RevisionId | None:
    if value is None:
        return None
    raw = _object(value, {"repository", "value"})
    return RevisionId(_repository(raw["repository"]), _text(raw["value"]))


def _revision_dict(value: RevisionId | None) -> dict[str, object] | None:
    if value is None:
        return None
    repository = value.repository
    return {
        "repository": {"host": repository.host, "owner": repository.owner, "name": repository.name},
        "value": value.value,
    }


def assignment_to_dict(assignment: Assignment) -> dict[str, object]:
    repository = assignment.authority.work.repository
    result: dict[str, object] = {
        "schema_version": 1,
        "id": assignment.id.value,
        "worker": {"namespace": assignment.worker.namespace, "value": assignment.worker.value},
        "objective": assignment.objective,
        "repository_context": assignment.repository_context,
        "change": assignment.change.key if assignment.change else None,
        "timeout_seconds": assignment.timeout_seconds,
        "authority": {
            "work": {
                "repository": {
                    "host": repository.host,
                    "owner": repository.owner,
                    "name": repository.name,
                },
                "key": assignment.authority.work.key,
            },
            "revision": _revision_dict(assignment.authority.revision),
            "permissions": sorted(assignment.authority.permissions),
            "expires_at": assignment.authority.expires_at,
        },
    }
    _secret_free(result)
    return result


def assignment_from_dict(value: object) -> Assignment:
    _secret_free(value)
    raw = _object(
        value,
        {
            "schema_version",
            "id",
            "worker",
            "objective",
            "repository_context",
            "change",
            "timeout_seconds",
            "authority",
        },
    )
    _version(raw)
    authority = _object(raw["authority"], {"work", "revision", "permissions", "expires_at"})
    work = _object(authority["work"], {"repository", "key"})
    repository = _repository(work["repository"])
    permissions = authority["permissions"]
    if not isinstance(permissions, list):
        raise ValueError("permissions must be an array")
    parsed = tuple(Authority(_text(item)) for item in permissions)
    if len(set(parsed)) != len(parsed):
        raise ValueError("duplicate permissions")
    return Assignment(
        AssignmentId(_text(raw["id"])),
        _worker(raw["worker"]),
        _text(raw["objective"]),
        AuthorityManifest(
            WorkId(repository, _text(work["key"])),
            _revision(authority["revision"]),
            frozenset(parsed),
            _integer(authority["expires_at"]),
        ),
        _text(raw["repository_context"]),
        ChangeId(repository, _text(raw["change"])) if raw["change"] is not None else None,
        _integer(raw["timeout_seconds"]),
    )


def resume_from_dict(value: object) -> ResumePacket:
    _secret_free(value)
    raw = _object(
        value,
        {
            "schema_version",
            "assignment",
            "attempt",
            "worker",
            "checkpoint",
            "updated_at",
            "revision",
            "completed_steps",
        },
    )
    _version(raw)
    steps = raw["completed_steps"]
    if not isinstance(steps, list):
        raise ValueError("completed steps must be an array")
    return ResumePacket(
        AssignmentId(_text(raw["assignment"])),
        AttemptId(_text(raw["attempt"])),
        _worker(raw["worker"]),
        _text(raw["checkpoint"]),
        _integer(raw["updated_at"]),
        _revision(raw["revision"]),
        tuple(_text(item) for item in steps),
    )


def process_request(
    assignment: Assignment, attempt: AttemptId, resume: ResumePacket | None = None
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "assignment": assignment_to_dict(assignment),
        "attempt": attempt.value,
        "prompt": assignment.prompt(),
        "resume": resume.to_dict() if resume else None,
    }


def execution_result_to_dict(result: ExecutionResult) -> dict[str, object]:
    value: dict[str, object] = {
        "outcome": result.outcome,
        "detail": result.detail,
        "revision": _revision_dict(result.revision),
        "evidence": [
            {
                "kind": item.kind,
                "revision": _revision_dict(item.revision),
                "source": item.source,
                "summary": item.summary,
            }
            for item in result.evidence
        ],
        "resume": result.resume.to_dict() if result.resume else None,
        "retry_after_seconds": result.retry_after_seconds,
    }
    _secret_free(value)
    return value


def process_response(
    assignment: Assignment, attempt: AttemptId, result: ExecutionResult
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "assignment": assignment.id.value,
        "attempt": attempt.value,
        "result": execution_result_to_dict(result),
    }


def process_result(value: object, assignment: Assignment, attempt: AttemptId) -> ExecutionResult:
    _secret_free(value)
    raw = _object(value, {"schema_version", "assignment", "attempt", "result"})
    _version(raw)
    if raw["assignment"] != assignment.id.value or raw["attempt"] != attempt.value:
        raise ValueError("process result belongs to another assignment or attempt")
    result = _object(
        raw["result"],
        {
            "outcome",
            "detail",
            "revision",
            "evidence",
            "resume",
            "retry_after_seconds",
        },
    )
    evidence_raw = result["evidence"]
    if not isinstance(evidence_raw, list):
        raise ValueError("evidence must be an array")
    evidence: list[RuntimeEvidence] = []
    for value in evidence_raw:
        item = _object(value, {"kind", "revision", "source", "summary"})
        revision = _revision(item["revision"])
        if revision is None:
            raise ValueError("evidence requires a revision")
        evidence.append(
            RuntimeEvidence(
                EvidenceType(_text(item["kind"])),
                revision,
                _text(item["source"]),
                _text(item["summary"]),
            )
        )
    parsed = ExecutionResult(
        Outcome(_text(result["outcome"])),
        _text(result["detail"]) if result["detail"] is not None else None,
        _revision(result["revision"]),
        tuple(evidence),
        resume_from_dict(result["resume"]) if result["resume"] is not None else None,
        _integer(result["retry_after_seconds"])
        if result["retry_after_seconds"] is not None
        else None,
    )
    if (
        parsed.revision is not None
        and parsed.revision.repository != assignment.authority.work.repository
    ):
        raise ValueError("process result escaped the assignment repository")
    if parsed.resume is not None and (
        parsed.resume.assignment != assignment.id
        or parsed.resume.attempt != attempt
        or parsed.resume.worker != assignment.worker
    ):
        raise ValueError("process resume belongs to another assignment, attempt or worker")
    return parsed
