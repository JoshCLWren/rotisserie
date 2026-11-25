#!/usr/bin/env python3
"""Incident mode: the operator diverts free workers to one issue until it is fixed.

``rotisserie factory incident start issue:N`` writes the repository variable
``FACTORY_INCIDENTS``, a comma list of ``ISSUE:ATTEMPTS`` entries such as
``239:3``. Every factory workflow receives it through ``FACTORY_REPO_VARS``,
so the next dispatcher tick obeys a change without a deploy. The reason lives
in ``FACTORY_INCIDENT_NOTES``, which only the operator console reads, and the
issue carries the visible ``factory:incident`` label.

An incident is active only while its issue is open. While one is active,
incident work ranks ahead of every other item, its issue bypasses intake
limits, and new non-incident issue intake pauses. Running work is never
cancelled. Malformed entries are ignored, so a bad value cannot pause the
factory. When the issue closes, intake resumes at once; ``reconcile`` (run by
the dispatcher) then closes the remaining incident PRs as superseded and
removes the label.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping
from typing import Any
from urllib.parse import quote

from factory_settings import repository_variables
from factory_work_policy_base import (
    labels_of,
    linked_issue_from_branch,
    linked_issue_from_pr,
    owner_of,
)

INCIDENTS_VAR = "FACTORY_INCIDENTS"
NOTES_VAR = "FACTORY_INCIDENT_NOTES"
LABEL = "factory:incident"
DEFAULT_ATTEMPTS = 3
MAX_ATTEMPTS = 6
ENTRY_RE = re.compile(r"^([1-9][0-9]{0,8})(?::([1-9][0-9]?))?$")
SUPERSEDED_MARKER = "factory-incident-superseded-v1"
HELD_LABELS = frozenset({"factory:claimed", "factory:override"})
CHECKPOINT_RE = re.compile(r"^factory/work-checkpoints/issue-([1-9][0-9]*)/")


def parse(raw: str) -> dict[int, int]:
    incidents: dict[int, int] = {}
    for part in raw.split(","):
        match = ENTRY_RE.fullmatch(part.strip())
        if match is None:
            continue
        attempts = int(match.group(2) or DEFAULT_ATTEMPTS)
        if 1 <= attempts <= MAX_ATTEMPTS:
            incidents[int(match.group(1))] = attempts
    return incidents


def format_incidents(incidents: Mapping[int, int]) -> str:
    return ",".join(f"{number}:{attempts}" for number, attempts in sorted(incidents.items()))


def declared(env: Mapping[str, str] | None = None) -> dict[int, int]:
    values = os.environ if env is None else env
    raw = values.get(INCIDENTS_VAR) or repository_variables(env).get(INCIDENTS_VAR, "")
    return parse(raw)


def _is_open(item: Mapping[str, Any]) -> bool:
    return str(item.get("state") or "OPEN").upper() == "OPEN"


def active(
    issues: Iterable[Mapping[str, Any]], incidents: Mapping[int, int] | None = None
) -> dict[int, int]:
    """Declared incidents whose issue is open and still carries the label.

    The label is removed when the incident ends, so a reopened issue does not
    restart an ended incident.
    """
    wanted = declared() if incidents is None else incidents
    live = {
        int(issue["number"])
        for issue in issues
        if _is_open(issue) and LABEL in labels_of(dict(issue))
    }
    return {number: attempts for number, attempts in wanted.items() if number in live}


def linked_issue(pr: Mapping[str, Any]) -> int | None:
    """The issue a PR works on, for incident ranking and holds.

    A checkpoint handoff PR (``factory/work-checkpoints/issue-N/...``) only
    refers to its issue, so ``linked_issue_from_pr`` misses it.
    """
    head = pr.get("headRefName")
    if head is None and isinstance(pr.get("head"), Mapping):
        head = pr["head"].get("ref")
    linked = linked_issue_from_pr({**pr, "headRefName": head})
    if linked is not None:
        return linked
    match = CHECKPOINT_RE.match(str(head or ""))
    return int(match.group(1)) if match else None


def incident_prs(
    prs: Iterable[Mapping[str, Any]], active_issues: Collection[int]
) -> frozenset[int]:
    """Open PRs linked to an active incident: owner-directed work no throttle holds."""
    if not active_issues:
        return frozenset()
    return frozenset(
        int(pr["number"]) for pr in prs if _is_open(pr) and linked_issue(pr) in active_issues
    )


def _attempt_prs(prs: Iterable[Mapping[str, Any]], number: int) -> list[Mapping[str, Any]]:
    return [
        pr
        for pr in prs
        if _is_open(pr) and linked_issue_from_branch(str(pr.get("headRefName") or "")) == number
    ]


def _held(pr: Mapping[str, Any]) -> bool:
    """An owner claim or a running worker keeps a PR open."""
    labels = labels_of(dict(pr))
    return bool(labels & HELD_LABELS) or owner_of(labels) not in (None, "factory:unowned")


def superseded_prs(prs: Iterable[Mapping[str, Any]], number: int) -> list[int]:
    """Open factory attempt PRs (factory/<worker>-<N>-...) nobody holds."""
    return sorted(int(pr["number"]) for pr in _attempt_prs(prs, number) if not _held(pr))


def held_prs(prs: Iterable[Mapping[str, Any]], number: int) -> list[int]:
    return sorted(int(pr["number"]) for pr in _attempt_prs(prs, number) if _held(pr))


def superseded_comment(number: int) -> str:
    return (
        f"Incident #{number} ended: the issue is closed, so this attempt is superseded "
        f"and closed. <!-- {SUPERSEDED_MARKER}:{number} -->"
    )


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def _end(repo: str, number: int, prs: list[dict[str, Any]], dry_run: bool) -> dict[str, Any]:
    closed = superseded_prs(prs, number)
    held = held_prs(prs, number)
    if not dry_run:
        for pr in closed:
            _gh("pr", "close", str(pr), "--repo", repo, "--comment", superseded_comment(number))
        if not held:
            try:
                label = quote(LABEL, safe="")
                _gh("api", "--method", "DELETE", f"repos/{repo}/issues/{number}/labels/{label}")
            except subprocess.CalledProcessError:
                pass
    return {"issue": number, "closed_prs": closed, "held_prs": held}


def reconcile(
    repo: str, incidents: Collection[int], *, dry_run: bool = False
) -> list[dict[str, Any]]:
    """End every declared incident whose issue has closed and still has the label.

    The label stays while an owner claim or a running worker holds an attempt
    PR, so a later tick closes that PR once it is free.
    """
    ended = []
    prs: list[dict[str, Any]] | None = None
    for number in sorted(incidents):
        issue = json.loads(_gh("api", f"repos/{repo}/issues/{number}"))
        if str(issue.get("state") or "").lower() != "closed" or LABEL not in labels_of(issue):
            continue
        if prs is None:
            prs = json.loads(
                _gh(
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--limit",
                    "500",
                    "--json",
                    "number,title,body,labels,headRefName,state",
                )
            )
        ended.append(_end(repo, number, prs, dry_run))
    return ended


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "reconcile"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    incidents = declared()
    if args.command == "list":
        print(
            json.dumps(
                {"incidents": [{"issue": n, "attempts": k} for n, k in sorted(incidents.items())]}
            )
        )
        return 0
    if not args.repo:
        parser.error("reconcile requires --repo or GITHUB_REPOSITORY")
    ended = reconcile(args.repo, incidents, dry_run=args.dry_run)
    for row in ended:
        print(
            f"[factory-incident] incident #{row['issue']} ended; superseded PRs: {row['closed_prs']}",
            file=sys.stderr,
        )
    print(json.dumps({"ended": ended}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
