#!/usr/bin/env python3
"""Keep a review assignment from running on a model that authored the PR.

The merge gate already refuses known self-review (see
``factory_review_policy.evaluate_review_eligibility``); this guard makes the
dispatcher avoid wasting a review session on one in the first place.

``authors`` runs once in the trusted validate job and reads the PR's comments a
single time; its JSON output travels to the selection step as a job output, so
re-picking a model costs no further GitHub calls. ``check`` is pure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence

import factory_review_policy as policy


def author_models_from_records(records: object, *, pr: int) -> list[str]:
    """Known author models from decoded issue-comment payloads (controller stream only)."""
    rows: list[dict[str, object]] = []
    pages = records if isinstance(records, list) else []
    for page in pages:
        for item in page if isinstance(page, list) else [page]:
            if isinstance(item, dict):
                rows.append(item)
    return policy.known_author_models(policy.controller_comment_bodies(rows), pr=pr)


def self_review(model: str, authors: Sequence[str]) -> list[str]:
    """The known author models that ``model`` is the same actual model as."""
    return [author for author in authors if policy.same_actual_model(model, author)]


def _authors(repo: str, pr: int) -> int:
    proc = subprocess.run(
        [
            "gh", "api", "--paginate", "--slurp",
            f"repos/{repo}/issues/{pr}/comments?per_page=100",
        ],
        text=True, capture_output=True, check=False, timeout=60,
    )  # fmt: skip
    if proc.returncode:
        print(f"review guard: comments unavailable: {proc.stderr.strip()}", file=sys.stderr)
        return 2
    print(json.dumps(author_models_from_records(json.loads(proc.stdout or "[]"), pr=pr)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    authors = commands.add_parser("authors", help="print the PR's known author models (JSON)")
    authors.add_argument("--repo", required=True)
    authors.add_argument("--pr", type=int, required=True)
    check = commands.add_parser(
        "check", help="exit 0 when MODEL is a known author (self-review), 1 when eligible"
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
        print(f"{args.model} authored this PR (as {', '.join(matches)})")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
