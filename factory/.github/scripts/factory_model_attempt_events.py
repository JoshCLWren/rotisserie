"""Reconstruct immutable model-attempt events from trusted completed Actions logs.

The model worker never receives a database credential. Only the post-run
collector (or deferred reconciler) reads the Actions job log and publishes these
observations. A worker-level terminal event remains separate from each model
attempt. Unknown outcomes stay unknown; a later model's success cannot be
attributed to a previous candidate.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STAMP = r"(?P<timestamp>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"
PREFIX = (
    r"\[factory:(?P<worker>\d+)\]"
    r"\[source:(?P<source>[a-z][a-z0-9-]*)\]"
    r"\[model:(?P<model>[^\]\r\n]{1,180})\]"
)
CANDIDATE = re.compile(
    r"(?m)^" + STAMP + r" " + PREFIX
    + r" FACTORY_EXECUTION_CANDIDATE: source=(?P=source)"
    + r" model=(?P=model) runtime=(?P<runtime>\S{1,240})\s*$"
)
PREFIXED_LINE = re.compile(r"(?m)^" + STAMP + r" " + PREFIX + r" (?P<message>[^\r\n]*)$")
EXIT = re.compile(r"^agent exit status (?P<code>-?\d+) for (?:pr|issue) #\d+\b")
FAILOVER = re.compile(
    r"^FACTORY_TRUSTED_FAILOVER: source=(?P<source>\S+)"
    r" model=(?P<model>\S+) outcome=(?P<outcome>[a-z_]+)"
    r"(?: scope=(?P<scope>account|model))?\s*$"
)
NO_CHANGE = re.compile(r"^FACTORY_NO_CHANGE_FAILOVER\b")
MAX_ATTEMPTS_PER_RUN = 2000


def _time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def model_attempt_events(
    run_id: str, outcome: dict[str, Any], job_log: str,
) -> list[dict[str, Any]]:
    """Emit one bounded, idempotent event per *observed* execution candidate.

    Timestamps and identities must come from the trusted Actions wrapper, not
    arbitrary model prose. The terminal classifier alone supplies the final
    candidate's outcome, and only when its exact canonical pair matches.
    """
    worker = str(outcome.get("worker") or "")
    if not worker.isdecimal() or not run_id.isdecimal():
        raise ValueError("invalid trusted model attempt identity")

    clean = ANSI.sub("", job_log)
    starts = [
        match for match in CANDIDATE.finditer(clean)
        if match.group("worker") == worker and _time(match.group("timestamp"))
    ]
    if len(starts) > MAX_ATTEMPTS_PER_RUN:
        raise ValueError("trusted model attempt count exceeds supported limit")
    if not starts:
        return []

    messages = [
        match for match in PREFIXED_LINE.finditer(clean)
        if match.group("worker") == worker and _time(match.group("timestamp"))
    ]
    events: list[dict[str, Any]] = []
    for index, start in enumerate(starts, 1):
        stop = starts[index].start() if index < len(starts) else len(clean)
        source, model = start.group("source"), start.group("model")
        started_at = start.group("timestamp")
        finished_at: str | None = None
        agent_exit_code: int | None = None
        attempt_outcome = "switched" if index < len(starts) else "unknown"
        evidence = "candidate_selection"
        for line in messages:
            if line.start() <= start.start() or line.start() >= stop:
                continue
            if line.group("source") != source or line.group("model") != model:
                continue
            message = line.group("message")
            failure = FAILOVER.fullmatch(message)
            exit_code = EXIT.match(message)
            if exit_code:
                agent_exit_code = int(exit_code.group("code"))
                finished_at = line.group("timestamp")
                evidence = "agent_exit"
            if NO_CHANGE.match(message):
                attempt_outcome = "no_progress"
                finished_at = line.group("timestamp")
                evidence = "no_change_failover"
            if failure and failure.group("source") == source and failure.group("model") == model:
                attempt_outcome = failure.group("outcome")
                finished_at = line.group("timestamp")
                evidence = "trusted_failover"

        if (
            index == len(starts)
            and source == outcome.get("source")
            and model == outcome.get("model")
            and outcome.get("exact_model_proven") is True
            and evidence not in {"trusted_failover", "no_change_failover"}
        ):
            attempt_outcome = str(outcome.get("outcome_class") or "unknown")
            evidence = "trusted_terminal"
        elapsed_ms: int | None = None
        if finished_at:
            start_time, finish_time = _time(started_at), _time(finished_at)
            if start_time and finish_time:
                delta = int((finish_time - start_time).total_seconds() * 1000)
                if delta >= 0:
                    elapsed_ms = delta
        events.append({
            "event_type": "model_attempt",
            "event_id": f"model-attempt:{run_id}:{index}",
            "worker": worker,
            "run_id": run_id,
            "attempt_index": index,
            "source": source,
            "model": model,
            "runtime_model": start.group("runtime"),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": elapsed_ms,
            "agent_exit_code": agent_exit_code,
            "outcome_class": attempt_outcome,
            "evidence": evidence,
            "selected_kind": outcome.get("selected_kind"),
            "selected_number": outcome.get("selected_number"),
            "observed_at": finished_at or started_at,
        })
    return events
