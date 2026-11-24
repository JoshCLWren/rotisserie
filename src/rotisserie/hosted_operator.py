"""Run one hosted control-room request inside a trusted GitHub Actions job.

The operator workflows call ``python -m rotisserie.hosted_operator read|write``.
The request is validated by the same contract as the local web UI, executed as
the shipped CLI with ``--json`` (never through a shell), and the result is
written as one JSON file that the workflow publishes as its run artifact. The
hosted page reads that file back through the adopter's sign-in service.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rotisserie.control_contract import (
    MAX_BODY,
    InvalidRequest,
    read_batch,
    request_id,
    write_command,
)

Runner = Callable[[str, list[str]], dict[str, Any]]
EXIT_REJECTED = 2
READ_TIMEOUT = 240
WRITE_TIMEOUT = 840


def _default_runner(timeout: int) -> Runner:
    from rotisserie.web import invoke

    return lambda repository, args: invoke(repository, args, timeout=timeout)


def _body(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > MAX_BODY:
        raise InvalidRequest(f"Factory request exceeds {MAX_BODY} bytes")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise InvalidRequest("Expected one JSON object") from exc
    if not isinstance(body, dict):
        raise InvalidRequest("Expected one JSON object")
    return body


def _exit_code(payload: dict[str, Any]) -> int:
    code = payload.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else 4


def execute(mode: str, raw: str, repository: str, runner: Runner) -> dict[str, Any]:
    """Validate and run one request. Always returns a result envelope."""
    envelope: dict[str, Any] = {"kind": mode, "request_id": ""}
    try:
        body = _body(raw)
        envelope["request_id"] = request_id(body)
        if mode == "read":
            reads = read_batch(body, trusted=True)
        else:
            if body.get("operation") == "read":
                raise InvalidRequest("Reads run in the read-only operator workflow")
            command = write_command(body)
            envelope["operation"] = body["operation"]
    except InvalidRequest as exc:
        envelope.update(exit_code=EXIT_REJECTED, error=f"Rejected factory command: {exc}")
        return envelope
    if mode == "read":
        results: list[dict[str, Any]] = []
        for name, query, args in reads:
            payload = runner(repository, args)
            results.append({"name": name, "query": query, "command": args, "result": payload})
        envelope["reads"] = results
        envelope["exit_code"] = max(_exit_code(item["result"]) for item in results)
    else:
        payload = runner(repository, command)
        envelope.update(command=command, result=payload, exit_code=_exit_code(payload))
    envelope["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return envelope


def main(argv: list[str] | None = None, *, runner: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rotisserie.hosted_operator")
    parser.add_argument("mode", choices=("read", "write"))
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    timeout = READ_TIMEOUT if arguments.mode == "read" else WRITE_TIMEOUT
    envelope = execute(
        arguments.mode,
        os.environ.get("ROTISSERIE_REQUEST", ""),
        repository,
        runner or _default_runner(timeout),
    )
    arguments.output.write_text(json.dumps(envelope, default=str), encoding="utf-8")
    summary = envelope.get("error") or (
        f"{arguments.mode} {envelope.get('operation') or ''} exit {envelope['exit_code']}"
    )
    print("Rotisserie hosted request:", summary)
    return 0 if arguments.mode == "read" and "error" not in envelope else envelope["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
