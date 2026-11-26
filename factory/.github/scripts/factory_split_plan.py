#!/usr/bin/env python3
"""A split plan for an oversized issue: the model's record, validated, and the child bodies.

A ``split`` run (``factory_self_heal_split`` dispatches it) asks one model to
divide an issue held for size or an epic-type label into 2 to 8 child issues.
The model ends its answer with ``FACTORY_SPLIT_PLAN`` and one JSON object:

    {"children": [{"title": "...", "body": "...", "acceptance": ["..."],
                   "files": ["path"], "blocked_by": [1]}]}

``blocked_by`` names earlier or later siblings by their 1-based position. The
worker wrapper validates the record and logs it once as ``FACTORY_SPLIT_PLAN:``.
The trusted Entry persister validates it again, checks that the dispatcher
leased this issue for a split, and records the plan in the store once per
parent. Trusted code writes every child body (``compose``) and checks it with
the factory's own parsers: under the size limit, not held for a split, not
manual-only, not an acceptance parent, and declaring exactly its sibling
blockers. Nothing here creates issues.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from factory_adopter import MARKER_PREFIX
from factory_issue_intake_guard import body_limit, split_reason
from factory_work_policy_base import eligibility

SENTINEL = "FACTORY_SPLIT_PLAN"
LOGGED = re.compile(rf"{SENTINEL}: (?P<plan>\{{.*\}})\s*$")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z ", re.MULTILINE)
MIN_CHILDREN, MAX_CHILDREN = 2, 8
MAX_CRITERIA = 3
MAX_FILES = 30
PLACEHOLDER = 9_999_999
CHILD_MARKER = "factory-split-child-v1"
DONE_MARKER = "factory-split-done-v1"
REJECTED_MARKER = "factory-split-rejected-v1"
FILE_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./*-]{0,199}")


class PlanError(ValueError):
    """The record is not a usable split plan."""


def child_marker(parent: int, index: int) -> str:
    return f"<!-- {MARKER_PREFIX}-{CHILD_MARKER}:parent-{parent}:index-{index} -->"


def is_child(body: str | None) -> bool:
    return f"{MARKER_PREFIX}-{CHILD_MARKER}:" in str(body or "")


def mission(parent: int, limit: int | None = None) -> str:
    cap = body_limit() if limit is None else limit
    return (
        f"Do not edit, commit or push anything. Read issue #{parent} and its comments with"
        f" `gh issue view {parent} --comments`, and the code it names. The issue is too large"
        " for one factory task. Split it into 2 to 8 child issues that together deliver all of"
        " it. Each child is one reviewable change: a title, a body that explains the change"
        f" (plain markdown, no HTML comments, well under {cap // 2} characters), 1 to"
        f" {MAX_CRITERIA} acceptance criteria that a reviewer can check independently, the 1 to"
        f" {MAX_FILES} repository files it touches, and the siblings that must land first (by"
        " 1-based position; no cycles). A glob such as `docs/api/*.json` counts as one file"
        " entry, so list many files of one kind as a glob. A change that still needs more"
        " entries is more than one child: split it into steps that each leave the repository"
        " working. If a factory comment says"
        " an earlier split plan was rejected, fix that reason. Do not add dependency lines, issue"
        " links or checklists to the body; the factory adds them. End your final response with"
        f" a line `{SENTINEL}` and then one JSON object:"
        ' {"children": [{"title": "...", "body": "...", "acceptance": ["..."],'
        ' "files": ["path/to/file"], "blocked_by": [1]}]}'
    )


def _text(value: object, name: str, most: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{name} is missing")
    text = value.strip()
    if len(text) > most:
        raise PlanError(f"{name} is longer than {most} characters")
    if "<!--" in text:
        raise PlanError(f"{name} contains an HTML comment")
    return text


def _order(blockers: Mapping[int, Sequence[int]]) -> list[int]:
    """Indexes with every blocker first; raises on a cycle."""
    done: list[int] = []
    state: dict[int, str] = {}

    def visit(index: int) -> None:
        if state.get(index) == "done":
            return
        if state.get(index) == "open":
            raise PlanError("the blockers form a cycle")
        state[index] = "open"
        for blocker in blockers[index]:
            visit(blocker)
        state[index] = "done"
        done.append(index)

    for index in sorted(blockers):
        visit(index)
    return done


def compose(parent: int, index: int, child: Mapping[str, Any], numbers: Mapping[int, int]) -> str:
    """The child issue body; ``numbers`` maps each blocking sibling index to its issue."""
    lines = [child_marker(parent, index), "", f"Part of #{parent}.", "", child["body"], ""]
    lines += ["## Acceptance criteria", ""]
    lines += [f"- {criterion}" for criterion in child["acceptance"]]
    lines += ["", "## Files", ""] + [f"- `{path}`" for path in child["files"]]
    blockers = [numbers[blocker] for blocker in child["blocked_by"]]
    if blockers:
        lines += ["", "Blocked by:"] + [f"- #{number}" for number in blockers]
    return "\n".join(lines) + "\n"


def check_body(body: str, blockers: set[int], limit: int) -> None:
    """The factory's own parsers must read the composed body as one plain task."""
    issue = {"body": body, "labels": [{"name": "factory"}]}
    if split_reason(issue, limit) is not None:
        raise PlanError("a child would itself need a split")
    if eligibility.is_manual_only(body) or eligibility.is_acceptance_parent(body):
        raise PlanError("a child reads as manual-only or as an acceptance parent")
    if eligibility.parse_declared_dependencies(body) != blockers:
        raise PlanError("a child declares dependencies other than its siblings")


def validate(raw: object, parent: int, limit: int | None = None) -> dict[str, Any]:
    """The normalized plan, with ``order`` (blockers first); raises ``PlanError``."""
    cap = body_limit() if limit is None else limit
    rows = raw.get("children") if isinstance(raw, Mapping) else None
    if not isinstance(rows, list) or not MIN_CHILDREN <= len(rows) <= MAX_CHILDREN:
        raise PlanError(f"a plan has {MIN_CHILDREN} to {MAX_CHILDREN} children")
    children: list[dict[str, Any]] = []
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise PlanError(f"child {position} is not an object")
        criteria = row.get("acceptance")
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= MAX_CRITERIA:
            raise PlanError(f"child {position} needs 1 to {MAX_CRITERIA} acceptance criteria")
        files = row.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise PlanError(f"child {position} needs 1 to {MAX_FILES} files")
        if not all(isinstance(path, str) and FILE_RE.fullmatch(path) for path in files):
            raise PlanError(f"child {position} names a file path the factory cannot accept")
        blocked = row.get("blocked_by", [])
        if not isinstance(blocked, list) or not all(
            type(item) is int and 1 <= item <= len(rows) and item != position for item in blocked
        ):
            raise PlanError(f"child {position} names a blocker that is not a sibling")
        children.append(
            {
                "title": " ".join(_text(row.get("title"), f"child {position} title", 200).split()),
                "body": _text(row.get("body"), f"child {position} body", cap),
                "acceptance": [
                    " ".join(_text(item, f"child {position} criterion", 500).split())
                    for item in criteria
                ],
                "files": list(dict.fromkeys(files)),
                "blocked_by": sorted(set(blocked)),
            }
        )
    order = _order({i: c["blocked_by"] for i, c in enumerate(children, start=1)})
    for index, child in enumerate(children, start=1):
        numbers = {blocker: PLACEHOLDER - blocker for blocker in child["blocked_by"]}
        body = compose(parent, index, child, numbers)
        check_body(body, set(numbers.values()), cap - 64)
    return {"parent": parent, "children": children, "order": order}


def from_agent_log(text: str) -> object:
    """The JSON object after the model's last sentinel line."""
    clean = ANSI.sub("", text)
    at = clean.rfind(SENTINEL)
    start = clean.find("{", at) if at >= 0 else -1
    if start < 0:
        raise PlanError("the model wrote no split plan")
    try:
        found, _end = json.JSONDecoder().raw_decode(clean[start:])
    except ValueError as exc:
        raise PlanError(f"the split plan is not JSON: {exc}") from None
    return found


def from_job_log(text: str) -> object:
    """The wrapper's newest logged plan line."""
    found = None
    for line in ANSI.sub("", text).splitlines():
        match = LOGGED.search(line)
        if match:
            found = match.group("plan")
    if found is None:
        raise PlanError("the job log has no logged split plan")
    return json.loads(found)


def _claimed_for_split(comments: Sequence[Mapping[str, Any]], issue: int, worker: str) -> bool:
    import factory_review_policy as policy

    pattern = re.compile(
        rf"{MARKER_PREFIX}-factory-controller-claim-v1:issue-{issue}:{re.escape(worker)}:"
        r"\d{10}(?P<fields>[^>]*)-->"
    )
    latest = None
    for body in policy.controller_comment_bodies(comments):
        for match in pattern.finditer(body):
            latest = match.group("fields")
    return latest is not None and ":action=split" in latest


def rejection(log: str, parent: int) -> str | None:
    """Why the model's plan in the job log fails validation, computed here, not read from the log."""
    try:
        validate(from_agent_log(STAMP.sub("", ANSI.sub("", log))), parent)
    except PlanError as exc:
        return str(exc)
    return None


def rejected_note(parent: int, run: str, reason: str) -> str:
    return (
        f"<!-- {MARKER_PREFIX}-{REJECTED_MARKER}:issue-{parent}:run-{run} -->\n\n"
        f"**Factory split:** the split plan from run {run} was rejected: {reason}. The next"
        " split run starts at once and must fix this reason. Each child is one reviewable"
        f" change with 1 to {MAX_FILES} file entries. A glob such as `docs/api/*.json` is one"
        " entry. A larger change becomes several children that each leave the repository"
        " working."
    )


def plan_event(plan: Mapping[str, Any], run: str) -> dict[str, Any]:
    parent = int(plan["parent"])
    return {
        "event_type": "factory_split",
        "event_id": f"split:plan:{parent}",
        "worker": "0",
        "kind": "plan",
        "issue": parent,
        "run": run,
        "plan": dict(plan),
    }


def persist(
    assignment: Mapping[str, Any],
    worker: str,
    outcome: Mapping[str, Any],
    log: str,
    comments: Sequence[Mapping[str, Any]],
    record: Any,
    run: str,
    note: Any = None,
) -> str:
    """Record a split run's plan once per parent, or why its plan failed; returns what happened.

    A rejected plan is a store event the sweep reads to retry at once, and a
    trusted comment on the parent that the next split run reads.
    """
    if assignment.get("kind") != "issue" or assignment.get("action") != "split":
        return "not a split run"
    issue = assignment.get("number")
    if type(issue) is not int or str(assignment.get("worker")) != worker:
        return "assignment does not match this worker"
    ended = outcome.get("outcome_class")
    if ended not in ("success", "work_failure"):
        return f"split run ended {ended or 'without an outcome'}"
    if not _claimed_for_split(comments, issue, worker):
        return "no trusted split lease for this worker"
    if ended == "work_failure":
        reason = rejection(log, issue)
        if reason is None:
            return "split run ended work_failure"
        entry = {
            "event_type": "factory_split",
            "event_id": f"split:rejected:{issue}:{run}",
            "worker": "0",
            "kind": "rejected",
            "issue": issue,
            "run": run,
            "reason": reason,
        }
        if record([entry]) and note is not None:
            note(rejected_note(issue, run, reason))
        return f"split plan rejected: {reason}"
    plan = validate(from_job_log(log), issue)
    inserted = record([plan_event(plan, run)])
    return f"recorded a {len(plan['children'])}-child plan" if inserted else "plan already recorded"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("mission").add_argument("parent", type=int)
    extract = commands.add_parser("extract", help="validate the model's plan from its agent log")
    extract.add_argument("--log", type=Path, required=True)
    extract.add_argument("--parent", type=int, required=True)
    keep = commands.add_parser("persist", help="trusted Entry: record a split run's plan")
    for name in ("--repo", "--run-id", "--worker", "--assignment"):
        keep.add_argument(name, required=True)
    keep.add_argument("--log", type=Path, required=True)
    keep.add_argument("--outcome", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "mission":
        print(mission(args.parent))
        return 0
    if args.command == "extract":
        try:
            text = args.log.read_text(encoding="utf-8", errors="replace")
            plan = validate(from_agent_log(text), args.parent)
        except PlanError as exc:
            print(f"split plan rejected: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(plan, separators=(",", ":")))
        return 0
    try:
        assignment = json.loads(args.assignment or "{}")
    except ValueError:
        assignment = {}
    if not isinstance(assignment, dict) or assignment.get("action") != "split":
        print("not a split run")
        return 0
    import factory_self_heal as heal
    import factory_state_ledger as ledger

    outcome = json.loads(args.outcome.read_text(encoding="utf-8"))
    issue = int(assignment.get("number") or 0)
    comments = heal.GhCli(args.repo).comments(issue) if issue > 0 else []
    try:
        result = persist(
            assignment,
            args.worker,
            outcome,
            args.log.read_text(encoding="utf-8", errors="replace"),
            comments,
            lambda entries: ledger.record_events(args.repo, entries),
            args.run_id,
            lambda body: heal.GhCli(args.repo).api(
                f"issues/{issue}/comments", "--method", "POST", payload={"body": body}
            ),
        )
    except PlanError as exc:
        result = f"split plan rejected: {exc}"
    print(result)
    return 0


if __name__ == "__main__":
    import factory_crash

    raise SystemExit(factory_crash.run(main))
