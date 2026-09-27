"""Command-line entrypoint for one bounded local operator operation."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rotisserie.application import CoordinationService, OperationStatus
from rotisserie.domain import (
    ChangeId,
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
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    correlation_id = arguments.correlation_id or str(uuid.uuid4())
    try:
        config = load_config(arguments.config.resolve())
        port = LocalCoordinationPort(config.snapshot, config.state_directory, config.repository)
        port.initialize()
        journal = OperationJournal(config.state_directory)
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
