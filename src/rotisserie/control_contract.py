"""The one command contract shared by the local and hosted control rooms.

The local web server, the GitHub operator workflows and the hosted page build
all read these tables, so their allowlists cannot drift. Requests are strict,
bounded command specifications, never shell input. Standard library only: the
Pages build loads this file by path without installing Rotisserie.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

TARGET = re.compile(r"^(?:issue|pr):[1-9][0-9]*$")
PR = re.compile(r"^pr:[1-9][0-9]*$")
ISSUE = re.compile(r"^issue:[1-9][0-9]*$")
ATTEMPTS = re.compile(r"^[1-6]$")
PROVIDER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
MODEL = re.compile(r"^[a-zA-Z0-9_./:+-]{1,180}$")
SETTING = re.compile(r"^[A-Z][A-Z0-9_]{1,100}$")
INTEGER = re.compile(r"^[0-9]{1,7}$")
REASON = re.compile(r"^[A-Za-z0-9 _.,:;()'/+#-]{0,180}$")
CHECKS = re.compile(r"^(?:pr:[1-9][0-9]*|main)$")
RUN_ID = re.compile(r"^[1-9][0-9]{0,19}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
ACTIONS = frozenset(
    ("advance", "implement", "review", "review-findings", "ci-repair", "merge-conflict")
)
LEVELS = frozenset(("critical", "high", "medium", "low", "none"))
READ_ONLY = frozenset(
    (
        "status",
        "recap",
        "report",
        "failures",
        "stats",
        "settings",
        "providers",
        "models",
        "why",
        "outcomes",
        "doctor",
        "leases",
        "checks",
        "review",
        "conflicts",
        "unstick",
        "runs",
        "follow",
        "history",
        "estimate",
        "incident",
        "limits",
        "backoffs",
        "db-usage",
        "store",
    )
)
# Served on GitHub Pages from the published snapshot without sign-in.
PUBLIC_READS = frozenset(("status", "report", "stats", "doctor", "outcomes", "db-usage"))
# Need a live evaluation: the hosted page runs them in the read-only workflow.
LIVE_READS = READ_ONLY - PUBLIC_READS
WRITE_OPERATIONS = frozenset(
    (
        "run",
        "cancel",
        "prioritize",
        "sync",
        "unstick",
        "settings",
        "provider_switch",
        "model_switch",
        "incident_start",
        "incident_stop",
        "doctor_fix",
        "release_expired",
        "claim",
        "finish",
        "labels",
        "estimate_neon",
        "close",
        "backoffs_reset",
        "store_selftest",
        "store_export",
    )
)
MAX_BODY = 4096
MAX_BATCH = 8
_READ_PARAMETERS = {
    "models": {"provider"},
    "why": {"target"},
    "checks": {"target"},
    "review": {"target"},
    "conflicts": {"target"},
    "runs": {"target"},
    "history": {"target"},
    "estimate": {"target", "sample", "run"},
    "follow": {"target"},
    "report": {"since"},
    "recap": {"since"},
    "failures": {"since"},
    "stats": {"window"},
    "store": {"mode", "run"},
}


class InvalidRequest(ValueError):
    """The browser submitted a command outside the supported UI contract."""


def _string(data: Mapping[str, Any], name: str, *, required: bool = False) -> str:
    value = data.get(name, "")
    if not isinstance(value, str) or len(value) > 180:
        raise InvalidRequest(f"Invalid {name}")
    if required and not value:
        raise InvalidRequest(f"Missing {name}")
    return value


def _match(value: str, pattern: re.Pattern[str], name: str) -> str:
    if not pattern.fullmatch(value):
        raise InvalidRequest(f"Invalid {name}")
    return value


def _flag(data: Mapping[str, Any], name: str) -> bool:
    value = data.get(name, False)
    if not isinstance(value, bool):
        raise InvalidRequest(f"Invalid {name}")
    return value


def read_command(name: str, query: Mapping[str, str], *, trusted: bool = False) -> list[str]:
    """Build only allowlisted, non-mutating factory commands.

    ``trusted`` is set only inside the read workflow, which holds the Neon
    connection; elsewhere Neon-only reads go through the trusted workflow.
    """
    if name not in READ_ONLY:
        raise InvalidRequest("Unknown read operation")
    if set(query) - _READ_PARAMETERS.get(name, set()):
        raise InvalidRequest("Unsupported query parameter")
    args = [name]
    if name == "models":
        args.append(_match(_string(query, "provider", required=True), PROVIDER, "provider"))
    elif name == "why":
        target = _string(query, "target")
        if target:
            args.append(_match(target, ISSUE, "target"))
    elif name == "checks":
        args.append(_match(_string(query, "target", required=True), CHECKS, "target"))
    elif name in ("review", "conflicts"):
        args.append(_match(_string(query, "target", required=True), PR, "target"))
    elif name in ("runs", "history"):
        args.append(_match(_string(query, "target", required=True), TARGET, "target"))
    elif name == "estimate":
        args.append(_match(_string(query, "target", required=True), TARGET, "target"))
        sample = _string(query, "sample")
        if sample:
            args.extend(("--sample", _match(sample, re.compile(r"^[0-9]{2,3}$"), "sample")))
        run = _string(query, "run")
        if run:
            args.extend(("--run", _match(run, RUN_ID, "run")))
    elif name == "follow":
        args.extend((_match(_string(query, "target", required=True), TARGET, "target"), "--once"))
    elif name == "store":
        mode = _string(query, "mode", required=True)
        if mode not in ("selftest", "export"):
            raise InvalidRequest("Invalid store mode")
        args.extend((mode, "--run", _match(_string(query, "run", required=True), RUN_ID, "run")))
    elif name in ("report", "failures", "recap"):
        since = _string(query, "since") or ("10h" if name == "failures" else "12h")
        args.extend(("--since", _match(since, re.compile(r"^[0-9]{1,3}[hd]$"), "since")))
    elif name == "outcomes":
        args.extend(("--since", "2h", "--limit", "40"))
    elif name == "stats":
        window = _string(query, "window") or "7d"
        if window not in ("24h", "7d"):
            raise InvalidRequest("Invalid window")
        args.extend(("--window", window))
    elif name == "unstick":
        args.append("--dry-run")
    elif name == "backoffs" and not trusted:
        args.extend(("--via", "github"))
    return args


def _run(data: Mapping[str, Any]) -> list[str]:
    provider = _match(_string(data, "provider", required=True), PROVIDER, "provider")
    model = _match(_string(data, "model") or "auto", MODEL, "model")
    args = ["run", provider, model, "--no-wait", "--yes"]
    target = _string(data, "target")
    action = _string(data, "action") or "advance"
    if action not in ACTIONS:
        raise InvalidRequest("Invalid action")
    if target:
        args.extend(("--target", _match(target, TARGET, "target")))
    if action != "advance":
        if not target or not target.startswith("pr:"):
            raise InvalidRequest("A specific pull request is required for this action")
        args.extend(("--action", action))
    if _flag(data, "force"):
        args.append("--force")
    if _flag(data, "take_over"):
        if not target:
            raise InvalidRequest("Takeover requires an issue or pull request")
        args.append("--take-over")
    slot = data.get("slot")
    if slot not in (None, ""):
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 1000:
            raise InvalidRequest("Invalid worker slot")
        args.extend(("--slot", str(slot)))
    return args


def _switch(name: str, data: Mapping[str, Any]) -> list[str]:
    words = ["enable" if _flag(data, "enabled") else "disable"]
    words.append(_match(_string(data, "provider", required=True), PROVIDER, "provider"))
    if name == "model_switch":
        words.append(_match(_string(data, "model", required=True), MODEL, "model"))
    reason = _match(_string(data, "reason"), REASON, "reason")
    if words[0] == "disable" and not reason.strip():
        raise InvalidRequest("Turning off needs a reason")
    command = "providers" if name == "provider_switch" else "models"
    return [command, *words, f"--reason={reason}"]


def _incident(name: str, data: Mapping[str, Any]) -> list[str]:
    issue = _match(_string(data, "issue", required=True), ISSUE, "issue")
    if name == "incident_stop":
        return ["incident", "stop", issue]
    args = [
        "incident",
        "start",
        issue,
        f"--reason={_match(_string(data, 'reason'), REASON, 'reason')}",
    ]
    attempts = _string(data, "attempts")
    if attempts:
        args.append(f"--attempts={_match(attempts, ATTEMPTS, 'attempts')}")
    return args


def _labels(data: Mapping[str, Any]) -> list[str]:
    mode = _string(data, "mode") or "dry-run"
    flags = {
        "dry-run": [],
        "repair": ["--repair"],
        "delete-legacy": ["--repair", "--delete-legacy"],
    }
    if mode not in flags:
        raise InvalidRequest("Invalid label doctor mode")
    return ["labels", *flags[mode], "--via", "github", "--no-wait"]


def _close(data: Mapping[str, Any]) -> list[str]:
    target = _match(_string(data, "target", required=True), PR, "target")
    reason = _match(_string(data, "reason", required=True), REASON, "reason")
    if not reason.strip():
        raise InvalidRequest("Closing needs a reason")
    args = ["close", target, f"--reason={reason}", "--yes"]
    if _flag(data, "keep_issue"):
        args.append("--keep-issue")
    return args


def write_command(data: Mapping[str, Any]) -> list[str]:
    """Explicit operations map to CLI guards. No arbitrary arguments accepted."""
    name = _string(data, "operation", required=True)
    if name not in WRITE_OPERATIONS:
        raise InvalidRequest("Unknown write operation")
    if name == "run":
        return _run(data)
    if name == "cancel":
        return [
            "cancel",
            _match(_string(data, "target", required=True), TARGET, "target"),
            "--no-wait",
        ]
    if name == "prioritize":
        target = _match(_string(data, "target", required=True), TARGET, "target")
        level = _string(data, "level") or "critical"
        if level not in LEVELS:
            raise InvalidRequest("Invalid priority")
        return ["prioritize", target, "--level", level]
    if name == "sync":
        return ["sync", _match(_string(data, "target", required=True), PR, "target"), "--yes"]
    if name == "unstick":
        return ["unstick", "--yes"]
    if name == "settings":
        setting = _match(_string(data, "name", required=True), SETTING, "setting")
        return [
            "settings",
            setting,
            _match(_string(data, "value", required=True), INTEGER, "value"),
        ]
    if name in ("provider_switch", "model_switch"):
        return _switch(name, data)
    if name in ("incident_start", "incident_stop"):
        return _incident(name, data)
    if name == "doctor_fix":
        return ["doctor", "--fix"]
    if name == "release_expired":
        return ["leases", "--release-expired"]
    if name == "estimate_neon":
        target = _match(_string(data, "target", required=True), TARGET, "target")
        return ["estimate", target, "--via", "github", "--no-wait"]
    if name in ("claim", "finish"):
        return [name, _match(_string(data, "target", required=True), TARGET, "target"), "--no-wait"]
    if name == "close":
        return _close(data)
    if name == "backoffs_reset":
        return ["backoffs", "reset", "--quota-only", "--yes", "--via", "github"]
    if name in ("store_selftest", "store_export"):
        return ["store", name.removeprefix("store_"), "--no-wait"]
    return _labels(data)


def request_id(data: Mapping[str, Any]) -> str:
    """The browser's correlation ID; optional so older callers keep working."""
    value = data.get("request_id", "")
    if value in ("", None):
        return ""
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise InvalidRequest("Invalid request_id")
    return value


def read_batch(
    data: Mapping[str, Any], *, trusted: bool = False
) -> list[tuple[str, dict[str, str], list[str]]]:
    """Validate ``{"operation": "read", "reads": [{"name", "query"}, ...]}``."""
    if data.get("operation") != "read":
        raise InvalidRequest("Expected a read request")
    reads = data.get("reads")
    if not isinstance(reads, list) or not 1 <= len(reads) <= MAX_BATCH:
        raise InvalidRequest(f"A read request holds 1 to {MAX_BATCH} reads")
    found: list[tuple[str, dict[str, str], list[str]]] = []
    for item in reads:
        if not isinstance(item, dict):
            raise InvalidRequest("Invalid read")
        name = _string(item, "name", required=True)
        query = item.get("query") or {}
        if not isinstance(query, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in query.items()
        ):
            raise InvalidRequest("Invalid read query")
        found.append((name, dict(query), read_command(name, query, trusted=trusted)))
    return found
