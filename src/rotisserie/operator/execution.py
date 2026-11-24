"""Durable, opt-in local worker execution behind the public operator surface."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

from rotisserie.adapters.process import ProcessExecutor
from rotisserie.application.process import (
    assignment_to_dict,
    execution_result_to_dict,
    process_result,
)
from rotisserie.application.runtime import (
    Assignment,
    AttemptEvent,
    AttemptId,
    ResumePacket,
    WorkerRuntime,
    require_secret_free,
    validate_assignment,
)
from rotisserie.domain import GraphSnapshot
from rotisserie.operator.config import ConfigurationError, OperatorConfig
from rotisserie.operator.records import OperationJournal


def execution_plan(assignment: Assignment, config: OperatorConfig) -> dict[str, object]:
    execution = config.execution
    if execution is None:
        raise ConfigurationError("execution is not configured")
    if assignment.authority.work.repository != config.repository:
        raise ValueError("assignment repository is outside configured scope")
    if config.workers is not None and assignment.worker not in config.workers.expected:
        raise ValueError("assignment worker is outside configured inventory")
    return {
        "schema_version": 1,
        "assignment": assignment.id.value,
        "work": assignment.authority.work.key,
        "worker": {"namespace": assignment.worker.namespace, "value": assignment.worker.value},
        "revision": assignment.authority.revision.value if assignment.authority.revision else None,
        "expires_at": assignment.authority.expires_at,
        "timeout_seconds": assignment.timeout_seconds,
        "executors": [item.name for item in execution.executors],
        "worktree": str(execution.worktree),
    }


def _validate_lease(assignment: Assignment, config: OperatorConfig, at: int) -> None:
    state = config.state_directory / "graph-state.json"
    try:
        raw = json.loads((state if state.exists() else config.snapshot).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("graph must be an object")
        if state.exists() and raw.get("schema_version") != 1:
            raise ValueError("unsupported graph state")
        snapshot = GraphSnapshot.from_dict(raw["snapshot"] if state.exists() else raw)
    except ValueError, TypeError, KeyError, RecursionError:
        raise ValueError("invalid execution graph") from None
    validate_assignment(assignment, snapshot, at=at)


def _validate_worktree(assignment: Assignment, config: OperatorConfig) -> None:
    execution = config.execution
    assert execution is not None
    worktree = execution.worktree
    # Inspect identity only after --apply and the execution gate. No tokens or
    # credential helpers are used, and raw command output never enters errors.
    environment = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(worktree), *args],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
        except OSError, subprocess.SubprocessError:
            raise ValueError("cannot verify execution worktree identity") from None
        return result.stdout.strip()

    if Path(git("rev-parse", "--show-toplevel")).resolve() != worktree:
        raise ValueError("execution.worktree must be the checkout root")
    remote = git("config", "--get", "remote.origin.url")
    if "://" in remote:
        parsed = urlsplit(remote)
        host, path = parsed.hostname, parsed.path.lstrip("/")
        if parsed.scheme not in {"https", "ssh"} or parsed.port is not None:
            raise ValueError("unsupported worktree remote transport")
        if (
            parsed.username not in {None, "git"}
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("worktree remote must not contain credentials or query data")
    else:
        left, separator, path = remote.partition(":")
        host = left.removeprefix("git@")
        if not separator or "/" in host or "@" in host:
            raise ValueError("cannot verify execution worktree remote")
    repository = config.repository
    if host != repository.host or path.rstrip("/").removesuffix(".git") != (
        f"{repository.owner}/{repository.name}"
    ):
        raise ValueError("worktree repository does not match configured target")
    revision = assignment.authority.revision
    if revision is not None and git("rev-parse", "HEAD") != revision.value:
        raise ValueError("worktree head does not match assignment authority")


@dataclass
class SignalCancellation:
    requested: bool = False

    def cancelled(self) -> bool:
        return self.requested

    def receive(self, number: int, frame: FrameType | None) -> None:
        del number, frame
        self.requested = True


@dataclass(frozen=True)
class JournalEvents:
    journal: OperationJournal
    correlation_id: str

    def record(self, event: AttemptEvent) -> None:
        self.journal.append(
            correlation_id=self.correlation_id,
            command="worker-event",
            dry_run=False,
            timestamp=event.recorded_at,
            result={
                "assignment": event.assignment.value,
                "attempt": event.attempt.value,
                "worker": {"namespace": event.worker.namespace, "value": event.worker.value},
                "status": event.state,
                "message": event.message,
                "progress": event.progress,
            },
        )


@dataclass
class ReceiptProcesses:
    path: Path
    receipt: dict[str, Any]

    def started(self, process_group: int) -> None:
        self.receipt["process_group"] = process_group
        _write_receipt(self.path, self.receipt)

    def stopped(self, process_group: int) -> None:
        del process_group
        self.receipt["process_group"] = None
        _write_receipt(self.path, self.receipt)


def execute_assignment(
    assignment: Assignment,
    config: OperatorConfig,
    journal: OperationJournal,
    correlation_id: str,
    *,
    resume: ResumePacket | None = None,
) -> dict[str, Any]:
    """Execute once under assignment, work-target and worker locks.

    An interrupted receipt is never silently retried; an explicit matching
    resume packet is required. A terminal receipt remains idempotent forever.
    """
    execution_plan(assignment, config)
    execution = config.execution
    assert execution is not None
    if not execution.enabled:
        raise ConfigurationError("execution is disabled; set execution.enabled=true to use --apply")
    if os.name != "posix":
        raise ConfigurationError("local worker execution requires POSIX")
    import fcntl

    directory = config.state_directory / "executions"
    directory.mkdir(parents=True, exist_ok=True)
    for parent in (directory.parent, directory.parent.parent):
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    identity = hashlib.sha256(assignment.id.value.encode()).hexdigest()
    digest = hashlib.sha256(
        json.dumps(assignment_to_dict(assignment), sort_keys=True).encode()
    ).hexdigest()
    receipt = directory / f"{identity}.json"
    # Separate target and worker locks suppress different assignment IDs that
    # would otherwise run concurrently against one target or reuse one worker.
    repository = config.repository
    scope = f"{repository.host}/{repository.owner}/{repository.name}"
    locks = {
        f"assignment:{identity}",
        f"work:{scope}:{assignment.authority.work.key}",
        f"worker:{scope}:{assignment.worker.namespace}:{assignment.worker.value}",
        f"worktree:{execution.worktree}",
    }
    with ExitStack() as stack:
        lock_fds: list[int] = []
        for key in sorted(locks):
            name = hashlib.sha256(key.encode()).hexdigest()
            stream = stack.enter_context((directory / f"{name}.lock").open("a"))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "assignment, target, worker or worktree is already executing"
                ) from None
            lock_fds.append(stream.fileno())
        if receipt.exists():
            prior = json.loads(receipt.read_text(encoding="utf-8"))
            if not isinstance(prior, dict) or type(prior.get("schema_version")) is not int:
                raise ValueError("invalid execution receipt")
            fields = {
                "schema_version",
                "assignment",
                "digest",
                "status",
                "replayed",
                "process_group",
            }
            if prior.get("status") == "completed":
                fields |= {"executor", "attempts", "result"}
            if set(prior) != fields or prior["schema_version"] != 1:
                raise ValueError("invalid execution receipt fields")
            if prior["assignment"] != assignment.id.value or prior["replayed"] is not False:
                raise ValueError("invalid execution receipt identity")
            if prior.get("digest") != digest:
                raise ValueError("assignment identity was reused with changed authority or content")
            if prior.get("status") == "completed":
                attempts = prior.get("attempts")
                if not isinstance(attempts, list) or not all(
                    isinstance(item, str) for item in attempts
                ):
                    raise ValueError("invalid terminal execution attempts")
                for identity in attempts:
                    require_secret_free(identity, "receipt attempt")
                    AttemptId(identity)
                executor = prior["executor"]
                if executor is not None:
                    if (
                        not isinstance(executor, str)
                        or not executor
                        or executor != executor.strip()
                    ):
                        raise ValueError("invalid terminal execution executor")
                    require_secret_free(executor, "receipt executor")
                if prior.get("process_group") is not None:
                    raise ValueError("terminal execution still records a process group")
                process_result(
                    {
                        "schema_version": 1,
                        "assignment": assignment.id.value,
                        "attempt": attempts[-1] if attempts else "cancelled-before-start",
                        "result": prior.get("result"),
                    },
                    assignment,
                    AttemptId(attempts[-1] if attempts else "cancelled-before-start"),
                )
                return {**prior, "replayed": True}
            if prior.get("status") != "started":
                raise ValueError("invalid execution receipt state")
            group = prior.get("process_group")
            if group is not None:
                if type(group) is not int or group <= 0:
                    raise ValueError("invalid interrupted execution process group")
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise ValueError("interrupted execution process group is still alive")
            if resume is None:
                raise ValueError("interrupted execution requires an explicit resume packet")
        elif resume is not None:
            raise ValueError("resume requires an interrupted execution receipt")
        if resume is not None and (
            resume.assignment != assignment.id
            or resume.worker != assignment.worker
            or resume.revision != assignment.authority.revision
        ):
            raise ValueError("resume packet does not match assignment authority")
        _validate_lease(assignment, config, int(time.time()))
        _validate_worktree(assignment, config)
        initial: dict[str, Any] = {
            "schema_version": 1,
            "assignment": assignment.id.value,
            "digest": digest,
            "status": "started",
            "replayed": False,
            "process_group": None,
        }
        _write_receipt(receipt, initial)
        cancellation = SignalCancellation()
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        for number in previous:
            signal.signal(number, cancellation.receive)
        try:
            runtime = WorkerRuntime(
                tuple(
                    ProcessExecutor(
                        item,
                        execution.worktree,
                        lock_fds=tuple(lock_fds),
                        observer=ReceiptProcesses(receipt, initial),
                    )
                    for item in execution.executors
                ),
                JournalEvents(journal, correlation_id),
            )
            result = runtime.run(
                assignment, at=int(time.time()), cancellation=cancellation, resume=resume
            )
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
        final = {
            **initial,
            "status": "completed",
            "executor": result.executor,
            "attempts": [item.value for item in result.attempts],
            "result": execution_result_to_dict(result.result),
        }
        _write_receipt(receipt, final)
        return final


def _write_receipt(path: Path, record: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".receipt-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
