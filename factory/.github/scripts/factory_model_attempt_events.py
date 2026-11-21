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
# Capture only diagnostic-shaped lines, never an arbitrary tail of agent output.
# Raw model output may contain credentials, prompts, user data or tool results.
MAX_FAILURE_TAIL_LINES = 8
MAX_FAILURE_TAIL_CHARS = 1600
MAX_FAILURE_LINE_CHARS = 240
LOG_PREFIX = re.compile(
    r"^\\[factory:(?P<worker>\\d+)\\]"
    r"\\[source:(?P<source>[^\\]\\r\\n]+)\\]"
    r"\\[model:(?P<model>[^\\]\\r\\n]+)\\] (?P<message>.*)$"
)
LOG_STAMP = re.compile(r"^\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d(?:\\.\\d+)?Z\\s+")
DIAGNOSTIC = re.compile(
    r"(?i)^(?:"
    r"(?:error|fatal|exception|warning|traceback|failure|failed|timeout|timed out)\\s*:"
    r"|(?:[!>]\\s*)?retrying\\s*\\(attempt\\s+\\d+\\)\\s*:"
    r"|(?:\\S+(?:Error|Exception))\\s*:"
    r"|FACTORY_TRUSTED_FAILOVER:"
    r"|FACTORY_NO_CHANGE_FAILOVER:"
    r"|agent exit status\\s+-?\\d+\\s+for\\s+(?:pr|issue)\\s+#\\d+"
    r"|semantic_review_recovery_failed:"
    r"|\\[?error\\]?:"
    r")"
)
# Avoid persisting an error message if it mentions secret-bearing parameters.
# This intentionally drops some potentially useful diagnostics rather than
# duplicating credentials or user-supplied content in the durable event ledger.
SECRET_CONTEXT = re.compile(
    r"(?i)(?:authorization|bearer|api[_-]?key|password|passwd|secret|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"private[_-]?key|database[_-]?url|connection[_-]?uri|"
    r"cookie|set-cookie|credential|\\bdsn\\b|"
    r"gh[pousr]_|github_pat_|sk-[a-z0-9]|xox[baprs]-)"
)
URL_OR_CONTACT = re.compile(r"(?i)(?:\\b(?:https?|postgres(?:ql)?)://\\S+|\\b\\S+@\\S+\\.\\w+)")
OPAQUE_VALUE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_-]{36,}(?![A-Za-z0-9_])")


def _sanitized_failure_tail(segment: str, worker: str, source: str, model: str) -> str | None:
    """Bounded, privacy-conscious diagnostics from *this* candidate only.

    Agent text is untrusted even inside a completed GitHub Actions log. Do not
    use this field for health decisions or save raw prompts, environment values,
    URLs or commands.
    """
    selected: list[str] = []
    for raw in segment.splitlines():
        line = LOG_STAMP.sub("", raw).strip()
        wrapper = LOG_PREFIX.match(line)
        if wrapper:
            if (wrapper.group("worker"), wrapper.group("source"), wrapper.group("model")) != (
                worker, source, model,
            ):
                continue
            line = wrapper.group("message").strip()
        if not DIAGNOSTIC.match(line) or SECRET_CONTEXT.search(line):
            continue
        # Keep recognizable error classes/status codes, but not arbitrary URLs
        # or long opaque values that could contain private credentials.
        line = URL_OR_CONTACT.sub("[redacted]", line)
        line = OPAQUE_VALUE.sub("[redacted]", line)
        line = line[:MAX_FAILURE_LINE_CHARS]
        if line and (not selected or selected[-1] != line):
            selected.append(line)
            if len(selected) > MAX_FAILURE_TAIL_LINES:
                selected.pop(0)
    return "\\n".join(selected)[-MAX_FAILURE_TAIL_CHARS:] or None


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
        failure_log_tail = None
        if attempt_outcome not in {"success", "no_work"}:
            failure_log_tail = _sanitized_failure_tail(
                clean[start.end():stop], worker, source, model,
            )
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
            "failure_log_tail": failure_log_tail,
            "selected_kind": outcome.get("selected_kind"),
            "selected_number": outcome.get("selected_number"),
            "observed_at": finished_at or started_at,
        })
    return events
