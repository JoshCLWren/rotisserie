"""The verdicts a factory worker prints, and what they mean to the console."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

from rotisserie.factory_console.github import Obj, string

OUTCOME_LINE = re.compile(r"\[factory-outcome\] (?P<verdict>[a-z_]+): (?P<message>.+)$", re.M)
TRACEBACK_ERROR = re.compile(
    r"^(?:\S+ )?(?:[A-Za-z_.]+(?:Error|Exception|Unavailable)): (.+)$", re.M
)
GENERIC_ANNOTATION = re.compile(r"^Process completed with exit code \d+\.?$")
OUTCOME_CLASS_LINE = re.compile(r"\[factory-outcome-class\] (?P<detail>\{.*\})\s*$", re.M)
FAILED_CLASS = re.compile(r"worker failed: ([a-z_0-9]+)")


class Result(StrEnum):
    PENDING = "pending"
    STARTED = "started"  # the model is working (default stopping point)
    DELIVERED = "delivered"  # a verified PR was handed off
    COMPLETED = "completed"  # finished without needing a PR (e.g. a review)
    IDLE = "idle"  # nothing useful to do
    NO_WORK = "no_work"  # nothing was assigned
    NO_CAPACITY = "no_capacity"  # no eligible model was free to take the work
    BUSY = "busy"  # the chosen item belongs to another worker
    FAILED = "failed"
    UNKNOWN = "unknown"  # finished but the result could not be read
    SUBMITTED = "submitted"  # --no-wait


EXIT_CODES = {
    Result.SUBMITTED: 0,
    Result.STARTED: 0,
    Result.DELIVERED: 0,
    Result.COMPLETED: 0,
    Result.IDLE: 0,
    Result.NO_WORK: 3,
    Result.NO_CAPACITY: 3,
    Result.BUSY: 3,
    Result.FAILED: 4,
    Result.UNKNOWN: 6,
    Result.PENDING: 6,
}


def failure_reasons(annotations: list[Obj], log: str = "") -> list[str]:
    """Specific error messages, without GitHub's generic exit-code line."""
    reasons: list[str] = []
    for note in annotations:
        if string(note, "annotation_level") != "failure":
            continue
        message = string(note, "message").strip()
        if message and not GENERIC_ANNOTATION.match(message) and message not in reasons:
            reasons.append(message)
    if not reasons and log:
        matches = TRACEBACK_ERROR.findall(log)
        if matches:
            reasons.append(matches[-1].strip())
    return reasons


@dataclass(frozen=True)
class WorkerOutcome:
    result: Result
    message: str
    pr: int | None = None


VERDICTS = {
    "code_delivered": Result.DELIVERED,
    "work_completed": Result.COMPLETED,
    "idle": Result.IDLE,
    "no_capacity": Result.NO_CAPACITY,
    "failed": Result.FAILED,
}


def parse_outcome(log: str) -> WorkerOutcome | None:
    match = None
    for match in OUTCOME_LINE.finditer(log):
        pass
    if match is None:
        return None
    message = match.group("message").strip()
    # "Verified PR #N was handed off" or "Code saved on the work PR; ... (work PR: N)".
    pr = re.search(r"PR #(\d+)|work PR: (\d+)", message)
    return WorkerOutcome(
        VERDICTS.get(match.group("verdict"), Result.UNKNOWN),
        message,
        int(pr.group(1) or pr.group(2)) if pr else None,
    )


LEGACY_CLASSES = {
    "code_delivered": "success",
    "work_completed": "success",
    "idle": "no_work",
    "no_capacity": "no_capacity",
}


def outcome_class(log: str) -> dict[str, str]:
    """The persisted outcome class, with source, model and target when printed.

    Runs from before the class line existed still give a class from the verdict.
    """
    found: dict[str, str] = {}
    for line in OUTCOME_CLASS_LINE.finditer(log):
        try:
            data = json.loads(line.group("detail"))
        except ValueError:
            continue
        if isinstance(data, dict):
            found = {
                key: str(data.get(key) or "") for key in ("class", "source", "model", "target")
            }
    if found.get("class"):
        return found
    verdict = None
    for verdict in OUTCOME_LINE.finditer(log):
        pass
    if verdict is None:
        return found
    failed = FAILED_CLASS.search(verdict.group("message"))
    name = failed.group(1) if failed else LEGACY_CLASSES.get(verdict.group("verdict"), "")
    return {**found, "class": name}
