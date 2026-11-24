#!/usr/bin/env python3
"""Decide what a run does when its pinned model cannot be selected.

A pinned model is a preference. When the provider's live catalog, the trusted
exclusions or the fleet switches no longer allow it, the run uses the best
eligible model from the same provider, picked by the normal selection path
(exclusions, health, fleet switches and the review author rule all apply). Only
an exact-or-nothing request keeps the hard failure: the dispatcher marks it
with ``exact_model`` in the assignment when the operator's model input starts
with ``exact:``. A provider with no eligible model at all is ``no_capacity``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

EXACT_PREFIX = "exact:"
EXACT_ERROR = "Exact requested model could not be selected; no provider fallback"


def split_request(model: str) -> tuple[str, bool]:
    """The model to request and whether the operator asked for exact-or-nothing."""
    text = (model or "").strip()
    if text.startswith(EXACT_PREFIX):
        return text.removeprefix(EXACT_PREFIX).strip(), True
    return text, False


def exact_from_assignment(raw: str) -> bool:
    try:
        context = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return False
    return isinstance(context, dict) and context.get("exact_model") is True


def unavailable(requested: str, reason: str, *, exact: bool) -> dict[str, str]:
    """The decision when the pinned model failed its live catalog check."""
    why = reason.strip().removeprefix("Exact model unavailable:").strip() or "not selectable"
    if exact:
        return {
            "action": "fail",
            "outcome": "model_unavailable",
            "detail": f"Operator-pinned provider/model unavailable: {why}",
            "error": EXACT_ERROR,
        }
    return {"action": "fallback", "reason": why, "requested": requested}


def exhausted(requested: str, source: str, reason: str, *, pin_reason: str) -> dict[str, str]:
    """The decision when the provider's own catalog has no eligible model."""
    if pin_reason:
        return {
            "outcome": "no_capacity",
            "detail": (
                f"Pinned model {requested} was unavailable ({pin_reason}) and {source} "
                f"has no other eligible model: {reason}"
            ),
            "error": f"No eligible {source} model replaces unavailable pinned model {requested}",
        }
    return {
        "outcome": "no_capacity",
        "detail": f"Pinned provider {source} has no eligible model: {reason}",
        "error": f"No eligible {source} model for the pinned provider; it is never switched",
    }


def note(requested: str, used: str, pin_reason: str) -> str:
    if not pin_reason or not requested or requested == "auto":
        return ""
    return (
        f"Pinned model {requested} was unavailable ({pin_reason}); "
        f"this run used {used} from the same provider."
    )


def comment(text: str, run_url: str) -> str:
    return f"**Factory model fallback:** {text}\n\nRun: {run_url}\n" if text else ""


def _write_outcome(path: str, decision: dict[str, str]) -> None:
    if path:
        Path(path).write_text(f"{decision['outcome']}\t{decision['detail']}\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("exact", help="print true when the assignment asks for exact-or-nothing")
    miss = commands.add_parser("unavailable", help="print the fallback reason, or fail")
    miss.add_argument("--requested-model", required=True)
    miss.add_argument("--exact", default="false")
    miss.add_argument("--error-file", default="")
    miss.add_argument("--outcome-file", default="")
    empty = commands.add_parser("exhausted", help="record why no model could be selected")
    empty.add_argument("--requested-model", required=True)
    empty.add_argument("--source", required=True)
    empty.add_argument("--reason", required=True)
    empty.add_argument("--pin-reason", default="")
    empty.add_argument("--outcome-file", default="")
    told = commands.add_parser("note", help="print the fallback note and PR comment as JSON")
    told.add_argument("--requested-model", required=True)
    told.add_argument("--used-model", required=True)
    told.add_argument("--pin-reason", default="")
    told.add_argument("--run-url", default="")
    args = parser.parse_args(argv)
    if args.command == "exact":
        print(
            "true" if exact_from_assignment(os.environ.get("FACTORY_ASSIGNMENT", "")) else "false"
        )
        return 0
    if args.command == "unavailable":
        error = Path(args.error_file).read_text(encoding="utf-8") if args.error_file else ""
        reason = error.strip().splitlines()[-1] if error.strip() else ""
        decision = unavailable(args.requested_model, reason, exact=args.exact == "true")
        if decision["action"] == "fail":
            _write_outcome(args.outcome_file, decision)
            print(decision["detail"], file=sys.stderr)
            return 1
        print(decision["reason"])
        return 0
    if args.command == "exhausted":
        decision = exhausted(
            args.requested_model, args.source, args.reason, pin_reason=args.pin_reason
        )
        _write_outcome(args.outcome_file, decision)
        print(f"::error::{decision['error']}")
        return 0
    text = note(args.requested_model, args.used_model, args.pin_reason)
    print(json.dumps({"note": text, "comment": comment(text, args.run_url)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
