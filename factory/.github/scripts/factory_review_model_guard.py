#!/usr/bin/env python3
"""Keep a review assignment from running on the model that authored the current head.

The merge gate already refuses known self-review (see
``factory_review_policy.evaluate_review_eligibility``); this guard makes the
dispatcher avoid wasting a review session on one in the first place. Only the
current head's author models are excluded (owner decision, 2026-11-24); see
``factory_review_exclusion`` for how a sync head carries its author.

``authors`` runs once in the trusted validate job and prints the excluded models
as JSON; that output travels to the selection step as a job output, so
re-picking a model costs no further GitHub calls. ``check`` is pure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence

import factory_review_exclusion as exclusion
import factory_review_policy as policy


def gh_fetch(repo: str) -> exclusion.Fetch:
    """A repository-relative REST reader over the ``gh`` CLI; failures raise."""

    def fetch(path: str) -> object:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/{path}"],
            text=True, capture_output=True, check=False, timeout=60,
        )  # fmt: skip
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or f"gh api {path} failed")
        return json.loads(proc.stdout or "null")

    return fetch


def self_review(model: str, authors: Sequence[str]) -> list[str]:
    """The excluded author models that ``model`` is the same actual model as."""
    return [author for author in authors if policy.same_actual_model(model, author)]


def _authors(repo: str, pr: int) -> int:
    try:
        found = exclusion.for_pull(gh_fetch(repo), pr)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"review guard: authorship unavailable: {exc}", file=sys.stderr)
        return 2
    print(f"review guard: {found.describe()}", file=sys.stderr)
    print(json.dumps(list(found.excluded)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    authors = commands.add_parser(
        "authors", help="print the models excluded from reviewing the PR's current head (JSON)"
    )
    authors.add_argument("--repo", required=True)
    authors.add_argument("--pr", type=int, required=True)
    check = commands.add_parser(
        "check", help="exit 0 when MODEL authored the current head (self-review), 1 when eligible"
    )
    check.add_argument("--authors-json", required=True)
    check.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    if args.command == "authors":
        return _authors(args.repo, args.pr)
    known = json.loads(args.authors_json or "[]")
    if not isinstance(known, list) or not all(isinstance(item, str) for item in known):
        print("review guard: invalid authors JSON", file=sys.stderr)
        return 2
    matches = self_review(args.model, known)
    if matches:
        print(f"{args.model} authored the current head (as {', '.join(matches)})")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
