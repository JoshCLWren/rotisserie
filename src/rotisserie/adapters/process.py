"""Opt-in POSIX worker processes; configured bridges are trusted executable code."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rotisserie.application.process import process_request, process_result
from rotisserie.application.runtime import (
    Assignment,
    AttemptEvent,
    AttemptId,
    AttemptState,
    Cancellation,
    EventSink,
    ExecutionResult,
    Outcome,
    ResumePacket,
    require_secret_free,
)


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        require_secret_free(self.name, "executor name")
        if not self.name or self.name != self.name.strip():
            raise ValueError("executor name must be non-empty and trimmed")
        if not self.argv or not Path(self.argv[0]).is_absolute():
            raise ValueError("executor requires an absolute executable path")
        if any(not item or "\x00" in item for item in self.argv):
            raise ValueError("executor arguments must be non-empty and NUL-free")
        for item in self.argv:
            require_secret_free(item, "executor argument")


class ProcessObserver(Protocol):
    def started(self, process_group: int) -> None: ...

    def stopped(self, process_group: int) -> None: ...


class ProcessExecutor:
    """Send one bounded JSON request on stdin and accept one bounded JSON response.

    No caller environment, raw stdout, or stderr is retained. Bridges adapt
    provider-specific APIs/CLIs outside this generic process lifecycle.
    """

    def __init__(
        self,
        spec: ProcessSpec,
        worktree: Path,
        *,
        output_limit: int = 1_048_576,
        heartbeat_seconds: float = 30,
        lock_fds: tuple[int, ...] = (),
        observer: ProcessObserver | None = None,
    ) -> None:
        if not worktree.is_absolute():
            raise ValueError("executor worktree must be absolute")
        if output_limit <= 0 or heartbeat_seconds <= 0:
            raise ValueError("process bounds must be positive")
        self._spec = spec
        self._worktree = worktree
        self._output_limit = output_limit
        self._heartbeat_seconds = heartbeat_seconds
        self._lock_fds = lock_fds
        self._observer = observer

    @property
    def name(self) -> str:
        return self._spec.name

    def execute(
        self,
        assignment: Assignment,
        attempt: AttemptId,
        *,
        cancellation: Cancellation,
        events: EventSink,
        resume: ResumePacket | None = None,
    ) -> ExecutionResult:
        if os.name != "posix":
            return ExecutionResult(Outcome.UNAVAILABLE, detail="POSIX process groups required")
        if cancellation.cancelled():
            return ExecutionResult(Outcome.CANCELLED)
        remaining = assignment.authority.expires_at - time.time()
        if remaining <= 0:
            return ExecutionResult(Outcome.TIMED_OUT, detail="assignment authority expired")
        deadline = time.monotonic() + min(assignment.timeout_seconds, remaining)
        next_heartbeat = time.monotonic() + self._heartbeat_seconds
        encoded = json.dumps(process_request(assignment, attempt, resume)).encode()
        if len(encoded) > self._output_limit:
            return ExecutionResult(Outcome.MALFORMED_OUTPUT, detail="process request exceeds bound")
        with tempfile.TemporaryDirectory(prefix="rotisserie-process-") as home:
            # A request file avoids blocking on a bridge that never reads stdin.
            with tempfile.TemporaryFile() as request:
                request.write(encoded)
                request.seek(0)
                environment = {
                    "PATH": "/usr/bin:/bin",
                    "HOME": home,
                    "XDG_CONFIG_HOME": home,
                    "XDG_CACHE_HOME": home,
                    "LANG": "C.UTF-8",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                }
                try:
                    process = subprocess.Popen(
                        self._spec.argv,
                        cwd=self._worktree,
                        env=environment,
                        stdin=request,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        pass_fds=self._lock_fds,
                    )
                except OSError:
                    return ExecutionResult(Outcome.UNAVAILABLE, detail="process could not start")
                try:
                    if self._observer is not None:
                        self._observer.started(process.pid)
                    return self._collect(
                        process, assignment, attempt, cancellation, events, deadline, next_heartbeat
                    )
                finally:
                    # Kill the entire session even if the leader exited, so an
                    # orphan cannot retain the worktree after authority ends.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    assert process.stdout is not None
                    process.stdout.close()
                    if self._observer is not None:
                        self._observer.stopped(process.pid)

    def _collect(
        self,
        process: subprocess.Popen[bytes],
        assignment: Assignment,
        attempt: AttemptId,
        cancellation: Cancellation,
        events: EventSink,
        deadline: float,
        next_heartbeat: float,
    ) -> ExecutionResult:
        assert process.stdout is not None
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                if cancellation.cancelled():
                    return ExecutionResult(Outcome.CANCELLED)
                current = time.monotonic()
                if current >= deadline or time.time() >= assignment.authority.expires_at:
                    return ExecutionResult(Outcome.TIMED_OUT)
                if current >= next_heartbeat:
                    events.record(
                        AttemptEvent(
                            assignment.id,
                            attempt,
                            assignment.worker,
                            AttemptState.HEARTBEAT,
                            int(time.time()),
                        )
                    )
                    next_heartbeat = current + self._heartbeat_seconds
                for key, _ in selector.select(min(0.1, max(0, deadline - current))):
                    chunk = os.read(key.fd, min(65_536, self._output_limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(process.stdout)
                    output.extend(chunk)
                    if len(output) > self._output_limit:
                        return ExecutionResult(
                            Outcome.MALFORMED_OUTPUT, detail="output exceeds bound"
                        )
                if not selector.get_map() and process.poll() is not None:
                    break
        if process.returncode != 0:
            return ExecutionResult(Outcome.FAILED, detail="process exited unsuccessfully")
        try:
            result = process_result(json.loads(output), assignment, attempt)
        except ValueError, TypeError, KeyError, RecursionError:
            return ExecutionResult(Outcome.MALFORMED_OUTPUT, detail="invalid process response")
        if cancellation.cancelled():
            return ExecutionResult(Outcome.CANCELLED)
        if time.monotonic() >= deadline or time.time() >= assignment.authority.expires_at:
            return ExecutionResult(Outcome.TIMED_OUT)
        return result
