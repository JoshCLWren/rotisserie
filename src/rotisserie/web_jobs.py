"""Factory commands the local web UI runs in the background and polls.

A write is never killed on a timeout: stopping ``unstick --yes`` half way
leaves some workers started and the operator unable to tell which. Reads are
safe to stop, so they keep a generous limit. The CLI's ``--progress`` lines
on stderr become the job's step list.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from rotisserie.factory_console.read_cache import ReadCache

READ_TIMEOUT = 600
KEEP_FINISHED = 20
MAX_RUNNING = 6
MAX_STEPS = 300


class JobRefused(RuntimeError):
    def __init__(self, message: str, running: str = "") -> None:
        super().__init__(message)
        self.running = running


def shared(args: list[str], max_age: int) -> list[str]:
    """Let a read reuse GitHub answers; the CLI refuses the cache for anything else."""
    return ["--max-age", str(max_age), *args] if max_age > 0 else list(args)


def factory_command(repository: str, args: list[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "rotisserie",
        "factory",
        "--repo",
        repository,
        "--json",
        "--progress",
        *args,
    ]


@dataclass
class Job:
    id: str
    args: list[str]
    mutating: bool
    started: float
    max_age: int = 0
    state: str = "running"
    finished: float | None = None
    result: dict[str, Any] | None = None
    steps: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_STEPS))
    stderr: deque[str] = field(default_factory=lambda: deque(maxlen=20))


class Jobs:
    def __init__(
        self,
        repository: str,
        *,
        command: Callable[[str, list[str]], list[str]] = factory_command,
        read_timeout: float = READ_TIMEOUT,
        keep: int = KEEP_FINISHED,
        clock: Callable[[], float] = time.monotonic,
        cache: ReadCache | None = None,
    ) -> None:
        self.repository = repository
        self.cache = cache or ReadCache.for_repo(repository, os.environ)
        self.command = command
        self.read_timeout = read_timeout
        self.keep = keep
        self.clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, args: list[str], *, mutating: bool, max_age: int = 0) -> Job:
        with self._lock:
            running = [job for job in self._jobs.values() if job.state == "running"]
            for job in running:
                if job.args == args:
                    return job
            if mutating and any(job.mutating for job in running):
                busy = next(job for job in running if job.mutating)
                raise JobRefused("Another change is still running. Wait for it to finish.", busy.id)
            if len(running) >= MAX_RUNNING:
                raise JobRefused("Too many factory commands are running. Try again shortly.")
            age = 0 if mutating else max_age
            job = Job(secrets.token_urlsafe(12), list(args), mutating, self.clock(), age)
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def view(self, job: Job) -> dict[str, Any]:
        with self._lock:
            end = job.finished if job.finished is not None else self.clock()
            return {
                "job": job.id,
                "command": job.args[0] if job.args else "",
                "state": job.state,
                "elapsed": round(end - job.started, 1),
                "steps": list(job.steps),
                "result": job.result,
            }

    def _run(self, job: Job) -> None:
        if job.mutating:
            self.cache.invalidate()
        try:
            self._execute(job)
        except Exception as exc:  # noqa: BLE001
            self._finish(job, "failed", _failure("unavailable", f"The job runner failed: {exc}"))
        finally:
            if job.mutating:
                self.cache.invalidate()

    def _execute(self, job: Job) -> None:
        try:
            process = subprocess.Popen(
                self.command(self.repository, shared(job.args, job.max_age)),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError:
            self._finish(job, "failed", _failure("unavailable", "Could not start Rotisserie."))
            return
        assert process.stdout is not None and process.stderr is not None
        reader = threading.Thread(target=self._read_steps, args=(job, process.stderr))
        reader.start()
        timer = None
        if not job.mutating:
            timer = threading.Timer(self.read_timeout, process.kill)
            timer.start()
        output = process.stdout.read()
        code = process.wait()
        reader.join()
        timed_out = False
        if timer is not None:
            timer.cancel()
            timed_out = code < 0
        if timed_out:
            limit = round(self.read_timeout)
            self._finish(
                job, "failed", _failure("timeout", f"The factory read took over {limit} seconds.")
            )
            return
        try:
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError("Not a JSON object")
        except ValueError:
            detail = " ".join(job.stderr)[-400:]
            message = "The factory did not return its JSON contract."
            self._finish(
                job,
                "failed",
                _failure("unavailable", message + (f" {detail}" if detail else ""), code),
            )
            return
        payload["exit_code"] = code
        self._finish(job, "finished", payload)

    def _read_steps(self, job: Job, stream: Iterable[str]) -> None:
        for line in stream:
            try:
                found = json.loads(line)
            except ValueError:
                found = None
            with self._lock:
                if isinstance(found, dict) and isinstance(found.get("step"), str):
                    job.steps.append({**found, "at": round(self.clock() - job.started, 1)})
                elif line.strip():
                    job.stderr.append(line.strip())

    def _finish(self, job: Job, state: str, result: dict[str, Any]) -> None:
        with self._lock:
            job.state = state
            job.result = result
            job.finished = self.clock()
            done = [item for item in self._jobs.values() if item.state != "running"]
            done.sort(key=lambda item: item.finished or 0.0)
            for old in done[: max(0, len(done) - self.keep)]:
                del self._jobs[old.id]


def _failure(result: str, error: str, code: int = 4) -> dict[str, Any]:
    return {"result": result, "error": error, "exit_code": code or 4}
