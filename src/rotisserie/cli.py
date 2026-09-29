"""Command-line entrypoint for one bounded local operator operation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rotisserie.adapters.github import (
    GitHubMutationAdapter,
    GitHubProjector,
    InMemoryMarkerStore,
    MutationPlan,
    MutationScope,
    MutationTarget,
)
from rotisserie.application import (
    AdoptionEvidence,
    AdoptionLane,
    AdoptionPolicy,
    AdoptionStage,
    CoordinationService,
    DecisionDimension,
    DecisionSnapshot,
    EffectCommand,
    GraphView,
    OperationStatus,
    ShadowReport,
    adoption_decision,
    compare_decisions,
    prepare_adoption_transition,
    project_decisions,
)
from rotisserie.domain import (
    ChangeId,
    GraphSnapshot,
    Lease,
    LeaseId,
    RevisionId,
    SchedulingPolicy,
    WorkerId,
    WorkId,
)
from rotisserie.operator import (
    ConfigurationError,
    LocalCoordinationPort,
    LocalStateError,
    OperationJournal,
    OperatorConfig,
    load_config,
)

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_REJECTED = 3
EXIT_FAILED = 4
MUTATING = frozenset({"claim", "run", "review", "complete", "recover"})


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="rotisserie")
    value.add_argument("--config", type=Path, required=True)
    value.add_argument("--correlation-id")
    subcommands = value.add_subparsers(dest="command", required=True)

    subcommands.add_parser("inspect", help="inspect the configured graph")
    plan = subcommands.add_parser("plan", help="plan deterministic work selection")
    plan.add_argument("--at", type=int, required=True)
    plan.add_argument("--completion-backlog", type=int, default=0)
    plan.add_argument("--max-active", type=int, default=1)
    plan.add_argument("--reserved-review", type=int, default=0)
    plan.add_argument("--backlog-limit", type=int)

    claim = subcommands.add_parser("claim", help="claim work with a bounded lease")
    _mutation_flag(claim)
    claim.add_argument("work")
    claim.add_argument("worker")
    claim.add_argument("--lease-id", required=True)
    claim.add_argument("--at", type=int, required=True)
    claim.add_argument("--expires-at", type=int, required=True)

    run = subcommands.add_parser("run", help="dispatch one already-claimed operation")
    _mutation_flag(run)
    run.add_argument("work")
    run.add_argument("worker")
    run.add_argument("--at", type=int, required=True)

    review = subcommands.add_parser("review", help="request independent exact-head review")
    _mutation_flag(review)
    review.add_argument("change")
    review.add_argument("revision")
    review.add_argument("reviewer")

    complete = subcommands.add_parser("complete", help="complete one ready exact-head change")
    _mutation_flag(complete)
    complete.add_argument("change")
    complete.add_argument("revision")

    recover = subcommands.add_parser("recover", help="release expired leases")
    _mutation_flag(recover)
    recover.add_argument("--at", type=int, required=True)

    doctor = subcommands.add_parser("doctor", help="validate config, state, and diagnostics")
    doctor.add_argument("--bundle", action="store_true")

    dogfood = subcommands.add_parser(
        "dogfood", help="produce non-activating GitHub projection and dry-run evidence"
    )
    dogfood.add_argument("--stage", choices=("fixture", "read-only", "dry-run"), required=True)
    dogfood.add_argument(
        "--payload",
        required=True,
        help="GitHub projection payload path, or - to read it from standard input",
    )
    dogfood.add_argument("--at", type=int, required=True)
    dogfood.add_argument("--max-active", type=int, default=1)
    dogfood.add_argument("--target", help="exact dry-run target as issue:NUMBER or change:NUMBER")
    dogfood.add_argument("--label", action="append", default=[])
    dogfood.add_argument("--state", choices=("open", "closed"), default="open")
    dogfood.add_argument("--expected-head")

    shadow = subcommands.add_parser(
        "shadow", help="compare normalized adopter and Rotisserie decisions"
    )
    shadow.add_argument(
        "--baseline", required=True, help="baseline decision snapshot path, or - for stdin"
    )

    decide = subcommands.add_parser(
        "decide", help="project normalized Rotisserie decisions from a graph snapshot"
    )
    decide.add_argument("--snapshot", required=True, help="graph snapshot path, or - for stdin")
    decide.add_argument("--revision", required=True, help="opaque adopter snapshot revision")
    decide.add_argument("--at", type=int, required=True)
    decide.add_argument("--completion-backlog", type=int, default=0)
    decide.add_argument("--backlog-limit", type=int)
    decide.add_argument("--active-changes", type=int, default=0)
    decide.add_argument("--wip-limit", type=int)
    shadow.add_argument(
        "--candidate", required=True, help="candidate decision snapshot path, or - for stdin"
    )

    shadow_project = subcommands.add_parser(
        "shadow-project",
        help="project Rotisserie decisions and compare them with an adopter baseline",
    )
    shadow_project.add_argument(
        "--baseline", required=True, help="baseline decision snapshot path, or - for stdin"
    )
    shadow_project.add_argument(
        "--snapshot", required=True, help="graph snapshot path, or - for stdin"
    )
    shadow_project.add_argument(
        "--revision", required=True, help="opaque adopter snapshot revision"
    )
    shadow_project.add_argument("--at", type=int, required=True)
    shadow_project.add_argument("--completion-backlog", type=int, default=0)
    shadow_project.add_argument("--backlog-limit", type=int)
    shadow_project.add_argument("--active-changes", type=int, default=0)
    shadow_project.add_argument("--wip-limit", type=int)

    adopt = subcommands.add_parser(
        "adopt", help="evaluate a non-mutating adopter cutover or rollback decision"
    )
    adopt.add_argument("--report", action="append", default=[], help="shadow report JSON path")
    adopt.add_argument("--lane", required=True)
    adopt.add_argument("--subject", action="append", required=True)
    adopt.add_argument("--minimum-matching-runs", type=int, default=1)
    adopt.add_argument(
        "--evidence", action="append", default=[], help="adoption evidence JSON path"
    )
    adopt.add_argument("--control-revision", required=True)
    adopt.add_argument(
        "--current-stage", choices=tuple(AdoptionStage), default=AdoptionStage.LEGACY
    )
    adopt.add_argument("--operator-approved", action="store_true")
    adopt.add_argument("--request-expansion", action="store_true")
    adopt.add_argument("--rollback-requested", action="store_true")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    correlation_id = arguments.correlation_id or str(uuid.uuid4())
    try:
        config = load_config(arguments.config.resolve())
        journal = OperationJournal(config.state_directory)
        if arguments.command == "dogfood":
            output = _dogfood(arguments, config, journal, correlation_id)
            status = EXIT_OK
        elif arguments.command == "shadow":
            output, status = _shadow(arguments, journal, correlation_id)
        elif arguments.command == "shadow-project":
            output, status = _shadow_project(arguments, config, journal, correlation_id)
        elif arguments.command == "decide":
            output, status = _decide(arguments, config, journal, correlation_id)
        elif arguments.command == "adopt":
            output, status = _adopt(arguments, journal, correlation_id)
        else:
            port = LocalCoordinationPort(config.snapshot, config.state_directory, config.repository)
            port.initialize()
            output, status = _execute(arguments, config, port, journal, correlation_id)
    except (ConfigurationError, LocalStateError, ValueError, OSError) as exc:
        _write(
            {
                "schema_version": 1,
                "status": "invalid",
                "error": str(exc),
                "correlation_id": correlation_id,
            }
        )
        return EXIT_INVALID
    _write(output)
    return status


def _execute(
    arguments: argparse.Namespace,
    config: OperatorConfig,
    port: LocalCoordinationPort,
    journal: OperationJournal,
    correlation_id: str,
) -> tuple[dict[str, Any], int]:
    command = str(arguments.command)
    service = CoordinationService(port)
    if command == "inspect":
        snapshot = port.read().snapshot
        return _envelope(
            correlation_id,
            command,
            "ok",
            graph=_graph_summary(snapshot.to_dict()),
        ), EXIT_OK
    if command == "plan":
        policy = SchedulingPolicy(
            arguments.max_active, arguments.reserved_review, arguments.backlog_limit
        )
        selected = service.select(
            at=arguments.at,
            completion_backlog=arguments.completion_backlog,
            policy=policy,
        )
        ready = service.completion_drain(limit=arguments.max_active)
        return _envelope(
            correlation_id,
            command,
            "ok",
            selected=[item.key for item in selected],
            ready_changes=[item.key for item in ready],
        ), EXIT_OK
    if command == "doctor":
        view = port.read()
        checks = {
            "configuration": "ok",
            "repository_scope": "ok",
            "local_state": "ok",
            "mutations": "enabled" if config.mutations_enabled else "disabled",
        }
        extra: dict[str, Any] = {"checks": checks, "metrics": journal.metrics()}
        if arguments.bundle:
            path = journal.diagnostic_bundle(
                config={
                    "repository": _repository_key(config),
                    "mutations_enabled": config.mutations_enabled,
                    "state_directory": str(config.state_directory),
                },
                graph=_graph_summary(view.snapshot.to_dict()),
            )
            extra["diagnostic_bundle"] = str(path)
        return _envelope(correlation_id, command, "ok", **extra), EXIT_OK
    assert command in MUTATING
    plan = _mutation_plan(arguments, config, port, correlation_id)
    if not arguments.apply:
        record = journal.append(
            correlation_id=correlation_id,
            command=command,
            dry_run=True,
            result=plan,
            timestamp=int(time.time()),
        )
        return _envelope(correlation_id, command, "planned", plan=plan, record=record), EXIT_OK
    if not config.mutations_enabled:
        raise ConfigurationError(
            "local mutations are disabled; set local.mutations_enabled=true to use --apply"
        )

    operation = correlation_id
    if command == "claim":
        result = service.claim(
            Lease(
                LeaseId(arguments.lease_id),
                _work(config, arguments.work),
                _worker(arguments.worker),
                arguments.at,
                arguments.expires_at,
            ),
            operation=operation,
            at=arguments.at,
        )
    elif command == "run":
        result = service.dispatch(
            _work(config, arguments.work),
            _worker(arguments.worker),
            operation=operation,
            at=arguments.at,
        )
    elif command == "review":
        result = service.review_transition(
            _change(config, arguments.change),
            RevisionId(config.repository, arguments.revision),
            _worker(arguments.reviewer),
            operation=operation,
        )
    elif command == "complete":
        result = service.completion(
            _change(config, arguments.change),
            RevisionId(config.repository, arguments.revision),
            operation=operation,
        )
    else:
        result = service.recover(operation=operation, at=arguments.at)
    record = journal.append(
        correlation_id=correlation_id,
        command=command,
        dry_run=False,
        result=result,
        timestamp=int(time.time()),
    )
    exit_code = EXIT_REJECTED if result.status is OperationStatus.REJECTED else EXIT_OK
    return _envelope(
        correlation_id,
        command,
        str(result.status),
        plan=plan,
        result=result.to_dict(),
        record=record,
    ), exit_code


def _dogfood(
    arguments: argparse.Namespace,
    config: OperatorConfig,
    journal: OperationJournal,
    correlation_id: str,
) -> dict[str, Any]:
    """Project externally acquired data and optionally build a credential-free plan."""

    try:
        raw_bytes = (
            sys.stdin.buffer.read()
            if arguments.payload == "-"
            else Path(arguments.payload).resolve().read_bytes()
        )
        raw = json.loads(raw_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read dogfood payload: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("dogfood payload root must be an object")
    snapshot = GitHubProjector(config.repository).project(raw)
    selected = CoordinationService(_ReadOnlyPort(snapshot)).select(
        at=arguments.at,
        completion_backlog=0,
        policy=SchedulingPolicy(arguments.max_active),
    )
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "stage": arguments.stage,
        "repository": _repository_key(config),
        "payload_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "graph": _graph_summary(snapshot.to_dict()),
        "selected": [item.key for item in selected],
        "remote_mutation": False,
        "activation_approved": False,
    }
    if arguments.stage == "dry-run":
        if not arguments.target:
            raise ConfigurationError("dry-run dogfood requires --target")
        target, number = _dogfood_target(arguments.target)
        if target is MutationTarget.CHANGE and not arguments.expected_head:
            raise ConfigurationError("change dry-run requires --expected-head")
        scope = MutationScope(
            config.repository,
            installation_id=0,
            issues=frozenset({number}) if target is MutationTarget.ISSUE else frozenset(),
            changes=frozenset({number}) if target is MutationTarget.CHANGE else frozenset(),
        )
        adapter = GitHubMutationAdapter(
            scope,
            _ForbiddenCredentials(),
            _ForbiddenTransport(),
            InMemoryMarkerStore(),
            dry_run=True,
        )
        adapter.execute(
            MutationPlan(
                config.repository,
                target,
                number,
                frozenset(arguments.label),
                arguments.state,
                arguments.expected_head,
            )
        )
        evidence["mutation_plans"] = list(adapter.public_plan_records())
    elif arguments.target or arguments.label or arguments.expected_head:
        raise ConfigurationError("mutation plan options are only valid for dry-run dogfood")

    record = journal.append(
        correlation_id=correlation_id,
        command=f"dogfood:{arguments.stage}",
        dry_run=True,
        result=evidence,
        timestamp=int(time.time()),
    )
    return _envelope(correlation_id, "dogfood", "ok", evidence=evidence, record=record)


def _shadow(
    arguments: argparse.Namespace,
    journal: OperationJournal,
    correlation_id: str,
) -> tuple[dict[str, Any], int]:
    """Compare two externally normalized snapshots without acquiring or mutating host state."""

    if arguments.baseline == "-" and arguments.candidate == "-":
        raise ConfigurationError("only one shadow snapshot may be read from standard input")
    baseline_bytes = _read_json_bytes(arguments.baseline, "baseline")
    candidate_bytes = _read_json_bytes(arguments.candidate, "candidate")
    baseline = DecisionSnapshot.from_dict(_json_object(baseline_bytes, "baseline"))
    candidate = DecisionSnapshot.from_dict(_json_object(candidate_bytes, "candidate"))
    report = compare_decisions(baseline, candidate)
    evidence = {
        "schema_version": 1,
        "baseline_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "report": report.to_dict(),
        "remote_mutation": False,
    }
    record = journal.append(
        correlation_id=correlation_id,
        command="shadow",
        dry_run=True,
        result=evidence,
        timestamp=int(time.time()),
    )
    status = "match" if report.matches else "diverged"
    exit_code = EXIT_OK if report.matches else EXIT_REJECTED
    return _envelope(
        correlation_id,
        "shadow",
        status,
        evidence=evidence,
        record=record,
    ), exit_code


def _decide(
    arguments: argparse.Namespace,
    config: OperatorConfig,
    journal: OperationJournal,
    correlation_id: str,
) -> tuple[dict[str, Any], int]:
    """Project policy from adopter-supplied graph data without host access or mutation."""

    snapshot_bytes = _read_json_bytes(arguments.snapshot, "graph snapshot")
    snapshot = GraphSnapshot.from_dict(_json_object(snapshot_bytes, "graph snapshot"))
    _validate_snapshot_repository(snapshot, config)
    decisions = project_decisions(
        snapshot,
        source="rotisserie",
        revision=arguments.revision,
        at=arguments.at,
        completion_backlog=arguments.completion_backlog,
        backlog_limit=arguments.backlog_limit,
        active_changes=arguments.active_changes,
        wip_limit=arguments.wip_limit,
    )
    evidence = {
        "schema_version": 1,
        "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "decisions": decisions.to_dict(),
        "remote_mutation": False,
    }
    record = journal.append(
        correlation_id=correlation_id,
        command="decide",
        dry_run=True,
        result=evidence,
        timestamp=int(time.time()),
    )
    return _envelope(correlation_id, "decide", "ok", evidence=evidence, record=record), EXIT_OK


def _shadow_project(
    arguments: argparse.Namespace,
    config: OperatorConfig,
    journal: OperationJournal,
    correlation_id: str,
) -> tuple[dict[str, Any], int]:
    """Atomically project and compare one adopter-supplied graph revision."""

    if arguments.baseline == "-" and arguments.snapshot == "-":
        raise ConfigurationError("only one shadow-project input may be read from standard input")
    baseline_bytes = _read_json_bytes(arguments.baseline, "baseline")
    snapshot_bytes = _read_json_bytes(arguments.snapshot, "graph snapshot")
    baseline = DecisionSnapshot.from_dict(_json_object(baseline_bytes, "baseline"))
    if baseline.revision != arguments.revision:
        raise ConfigurationError("baseline must describe the requested graph revision")
    snapshot = GraphSnapshot.from_dict(_json_object(snapshot_bytes, "graph snapshot"))
    _validate_snapshot_repository(snapshot, config)
    candidate = project_decisions(
        snapshot,
        source="rotisserie",
        revision=arguments.revision,
        at=arguments.at,
        completion_backlog=arguments.completion_backlog,
        backlog_limit=arguments.backlog_limit,
        active_changes=arguments.active_changes,
        wip_limit=arguments.wip_limit,
    )
    report = compare_decisions(baseline, candidate)
    evidence = {
        "schema_version": 1,
        "baseline_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "candidate": candidate.to_dict(),
        "report": report.to_dict(),
        "remote_mutation": False,
    }
    record = journal.append(
        correlation_id=correlation_id,
        command="shadow-project",
        dry_run=True,
        result=evidence,
        timestamp=int(time.time()),
    )
    status = "match" if report.matches else "diverged"
    exit_code = EXIT_OK if report.matches else EXIT_REJECTED
    return _envelope(
        correlation_id,
        "shadow-project",
        status,
        evidence=evidence,
        record=record,
    ), exit_code


def _validate_snapshot_repository(snapshot: GraphSnapshot, config: OperatorConfig) -> None:
    repositories = {item.id.repository for item in snapshot.works}
    repositories.update(item.id.repository for item in snapshot.changes)
    repositories.update(item.id.repository for item in snapshot.revisions)
    if repositories - {config.repository}:
        raise ConfigurationError("graph snapshot is outside the configured repository")


def _read_json_bytes(value: str, label: str) -> bytes:
    try:
        return sys.stdin.buffer.read() if value == "-" else Path(value).resolve().read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"cannot read {label}: {exc}") from exc


def _adopt(
    arguments: argparse.Namespace,
    journal: OperationJournal,
    correlation_id: str,
) -> tuple[dict[str, Any], int]:
    """Evaluate cutover evidence while leaving enforcement to the adopter."""

    report_bytes = [_read_json_bytes(path, "adoption report") for path in arguments.report]
    reports = tuple(
        ShadowReport.from_dict(_json_object(raw, "adoption report")) for raw in report_bytes
    )
    evidence_bytes = [_read_json_bytes(path, "adoption evidence") for path in arguments.evidence]
    adoption_evidence = tuple(
        AdoptionEvidence.from_dict(_json_object(raw, "adoption evidence")) for raw in evidence_bytes
    )
    decision = adoption_decision(
        reports,
        AdoptionLane(arguments.lane, tuple(arguments.subject)),
        adoption_evidence,
        policy=AdoptionPolicy(
            minimum_matching_runs=arguments.minimum_matching_runs,
            required_dimensions=frozenset(DecisionDimension),
        ),
        control_revision=arguments.control_revision,
        current_stage=AdoptionStage(arguments.current_stage),
        operator_approved=arguments.operator_approved,
        request_expansion=arguments.request_expansion,
        rollback_requested=arguments.rollback_requested,
    )
    evidence = {
        "schema_version": 1,
        "report_sha256": [hashlib.sha256(raw).hexdigest() for raw in report_bytes],
        "evidence_sha256": [hashlib.sha256(raw).hexdigest() for raw in evidence_bytes],
        "control_revision": arguments.control_revision,
        "decision": decision.to_dict(),
        "transition": (
            prepare_adoption_transition(
                decision,
                control_revision=arguments.control_revision,
            ).to_dict()
            if decision.authorized
            else None
        ),
        "remote_mutation": False,
    }
    record = journal.append(
        correlation_id=correlation_id,
        command="adopt",
        dry_run=True,
        result=evidence,
        timestamp=int(time.time()),
    )
    status = "authorized" if decision.authorized else "held"
    exit_code = EXIT_OK if decision.authorized else EXIT_REJECTED
    return _envelope(correlation_id, "adopt", status, evidence=evidence, record=record), exit_code


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"cannot parse {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} root must be an object")
    return value


class _ReadOnlyPort:
    def __init__(self, snapshot: GraphSnapshot) -> None:
        self._snapshot = snapshot

    def read(self) -> GraphView:
        return GraphView(self._snapshot, "dogfood-read-only")

    def apply(self, command: EffectCommand) -> None:
        del command
        raise AssertionError("dogfood projection must not apply coordination effects")


class _ForbiddenCredentials:
    def token_for(self, repository: Any, installation_id: int) -> Any:
        del repository, installation_id
        raise AssertionError("dry-run dogfood must not request credentials")


class _ForbiddenTransport:
    def inspect(self, *args: Any) -> Any:
        del args
        raise AssertionError("dry-run dogfood must not inspect remote state")

    def reconcile(self, *args: Any) -> Any:
        del args
        raise AssertionError("dry-run dogfood must not mutate remote state")


def _dogfood_target(value: str) -> tuple[MutationTarget, int]:
    kind, separator, raw_number = value.partition(":")
    try:
        target = MutationTarget(kind)
        number = int(raw_number)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("target must be issue:NUMBER or change:NUMBER") from exc
    if not separator or number <= 0:
        raise ConfigurationError("target must be issue:NUMBER or change:NUMBER")
    return target, number


def _mutation_plan(
    arguments: argparse.Namespace,
    config: OperatorConfig,
    port: LocalCoordinationPort,
    correlation_id: str,
) -> dict[str, Any]:
    target: dict[str, Any] = {"repository": _repository_key(config)}
    for field in ("work", "worker", "change", "revision", "reviewer", "at", "expires_at"):
        if hasattr(arguments, field):
            target[field] = getattr(arguments, field)
    return {
        "schema_version": 1,
        "operation": arguments.command,
        "correlation_id": correlation_id,
        "expected_graph_version": port.read().version,
        "target": target,
        "effects": [str(arguments.command)],
        "dry_run": True,
    }


def _graph_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": snapshot["schema_version"],
        "works": len(snapshot["works"]),
        "changes": len(snapshot["changes"]),
        "leases": len(snapshot["leases"]),
        "checks": len(snapshot["checks"]),
        "reviews": len(snapshot["reviews"]),
    }


def _work(config: OperatorConfig, key: str) -> WorkId:
    return WorkId(config.repository, key)


def _change(config: OperatorConfig, key: str) -> ChangeId:
    return ChangeId(config.repository, key)


def _worker(value: str) -> WorkerId:
    namespace, separator, identity = value.partition(":")
    if not separator:
        raise ValueError("worker must use namespace:value form")
    return WorkerId(namespace, identity)


def _repository_key(config: OperatorConfig) -> str:
    repository = config.repository
    return f"{repository.host}/{repository.owner}/{repository.name}"


def _mutation_flag(value: argparse.ArgumentParser) -> None:
    value.add_argument(
        "--apply",
        action="store_true",
        help="apply the planned local mutation (default is dry-run)",
    )


def _envelope(correlation_id: str, command: str, status: str, **values: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command": command,
        "status": status,
        "correlation_id": correlation_id,
        **values,
    }


def _write(value: dict[str, Any]) -> None:
    json.dump(value, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
