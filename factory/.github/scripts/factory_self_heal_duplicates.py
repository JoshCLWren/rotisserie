#!/usr/bin/env python3
"""Duplicate PRs resolve themselves: the factory keeps one PR per issue and closes the rest.

Two or more open PRs that deliver the same issue (the ``factory:duplicate``
rule, ``factory_state_labels.duplicate_groups``) are one too many. The sweep
keeps the PR with green CI at its head; among several (or none), the one with
fewer open blocking findings (unresolved review threads); then the older one.

Before it closes a duplicate, the sweep comments on it with a link to the kept
PR and the files it changed that the kept PR does not, and leaves the same list
on the kept PR as a trusted comment that the next repair run reads as context
(``factory_duplicate_context``). The closed branch stays. Only PRs that claim
the whole issue are closed: a ``Part of #N`` slice or a checkpoint branch never
is. A PR that a worker holds (a factory owner label) or an outside agent holds
(``agent:*``) waits for its release, and so does its whole group. The store
event comes last, so a failed pass is finished by the next one, and a PR the
factory closed once and someone reopened stays open.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import factory_duplicate_context as duplicate_context
import factory_self_heal as heal
import factory_self_heal_rules as rules
from factory_issue_intake_guard import CHECKPOINT_BRANCH_RE, PART_OF_RE
from factory_work_policy_base import linked_issue_from_pr, owner_of

MAX_GROUPS = 2
MAX_FILES = 40
GREEN = frozenset({"success", "skipped", "neutral"})
THREADS = """query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $pr) {
    reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes {
      author { login } } } } } } } }"""
IGNORED = frozenset({"coderabbitai", "coderabbitai[bot]"})


def _head(row: Mapping[str, Any]) -> str:
    head = row.get("head")
    return str(head.get("sha") or "") if isinstance(head, Mapping) else ""


def held(row: Mapping[str, Any]) -> str:
    names = heal.labels(row)
    agents = sorted(name for name in names if name.startswith("agent:"))
    if agents:
        return f"outside agent {agents[0]}"
    owner = owner_of(names)
    return f"worker {owner}" if owner not in (None, "factory:unowned") else ""


def green(github: heal.GitHub, head: str) -> bool:
    rows = github.api(f"commits/{head}/check-runs?per_page=100")
    runs = rows.get("check_runs") if isinstance(rows, Mapping) else None
    if not isinstance(runs, list) or not runs:
        return False
    return all(
        run.get("status") == "completed" and run.get("conclusion") in GREEN for run in runs
    )


def open_findings(github: heal.GitHub, number: int) -> int:
    owner, name = github.repo.split("/", 1)
    data = github.graphql(THREADS, owner=owner, name=name, pr=number)
    threads = (
        (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
        .get("reviewThreads", {})
        .get("nodes")
        or []
    )
    count = 0
    for thread in threads:
        first = ((thread.get("comments") or {}).get("nodes") or [{}])[0]
        login = str((first.get("author") or {}).get("login") or "")
        if not thread.get("isResolved") and login not in IGNORED:
            count += 1
    return count


def keeper(github: heal.GitHub, rows: Sequence[Mapping[str, Any]]) -> tuple[int, str]:
    """The PR to keep and why: green CI at head, then fewer open findings, then older."""
    passing = {int(row["number"]): green(github, _head(row)) for row in rows}
    pool = [row for row in rows if passing[int(row["number"])]] or list(rows)
    if len(pool) == 1:
        return int(pool[0]["number"]), "green CI at its head"
    findings = {int(row["number"]): open_findings(github, int(row["number"])) for row in pool}
    fewest = min(findings.values())
    pool = [row for row in pool if findings[int(row["number"])] == fewest]
    if len(pool) == 1:
        return int(pool[0]["number"]), f"fewest open review findings ({fewest})"
    oldest = min(pool, key=lambda row: (str(row.get("created_at") or ""), int(row["number"])))
    basis = "green CI, " if any(passing.values()) else ""
    return int(oldest["number"]), f"{basis}{fewest} open findings, and the oldest"


def files(github: heal.GitHub, number: int) -> list[str]:
    rows = github.api(f"pulls/{number}/files?per_page=100")
    return [str(row.get("filename")) for row in rows if isinstance(row, Mapping)] if isinstance(
        rows, list
    ) else []


def closed_before(repo: str) -> set[int]:
    """PRs the factory already closed as duplicates; a reopened one stays open."""
    import factory_turso_core as core

    rows = core.store().reads(
        [(
            "SELECT detail ->> 'pr' AS pr FROM factory_state_events WHERE repo = :repo"
            " AND event_type = 'factory_self_heal' AND detail ->> 'kind' = :kind",
            {"repo": repo, "kind": rules.DUPLICATE_CLOSED},
        )]
    )[0]
    return {int(row["pr"]) for row in rows if str(row["pr"] or "").isdecimal()}


def claims_whole(row: Mapping[str, Any], issue: int) -> bool:
    """The PR claims the whole issue: never a ``Part of`` slice or a checkpoint branch."""
    branch = str((row.get("head") or {}).get("ref") or "")
    if PART_OF_RE.search(str(row.get("body") or "")) or CHECKPOINT_BRANCH_RE.match(branch):
        return False
    shaped = {"headRefName": branch, "body": row.get("body"), "title": row.get("title")}
    return linked_issue_from_pr(shaped) == issue


def close_duplicate(
    github: heal.GitHub, store: heal.Store, issue: int, kept: int, why: str,
    pull: Mapping[str, Any], kept_files: set[str],
) -> dict[str, Any]:
    """Comment on both PRs and close; the store event comes last, and each step repeats safely."""
    number, head = int(pull["number"]), _head(pull)
    branch = (pull.get("head") or {}).get("ref") or ""
    extra = [path for path in files(github, number) if path not in kept_files]
    listed = ", ".join(f"`{path}`" for path in extra[:MAX_FILES]) or "none"
    more = f" and {len(extra) - MAX_FILES} more" if len(extra) > MAX_FILES else ""
    text = (
        f"Closed #{number} as a duplicate: #{kept} delivers the same issue #{issue} and stays"
        f" ({why}). Files this PR changed that #{kept} does not: {listed}{more}. The branch"
        f" `{branch}` stays. Reopen this PR to undo."
    )
    ref = f"dup-{kept}-{number}"
    if not any(
        row["kind"] == rules.DUPLICATE_CLOSED and row["ref"] == ref
        for row in rules.heals(github.comments(number), pr=number)
    ):
        body = rules.comment_body(
            kind=rules.DUPLICATE_CLOSED, pr=number, head=head, ref=ref, text=text
        )
        github.api(f"issues/{number}/comments", "--method", "POST", payload={"body": body})
    marker = duplicate_context.marker(kept, number)
    if not any(marker in str(row.get("body") or "") for row in github.comments(kept)):
        context = (
            f"{marker}\n\n{duplicate_context.HEADLINE} #{number} was closed as a duplicate of"
            f" this PR (issue #{issue}). It changed files this PR does not: {listed}{more}. The"
            f" next repair reads them from branch `{branch}` and ports what this PR needs."
        )
        github.api(f"issues/{kept}/comments", "--method", "POST", payload={"body": context})
    github.api(f"pulls/{number}", "--method", "PATCH", payload={"state": "closed"})
    store.record(
        [heal.event(kind=rules.DUPLICATE_CLOSED, pr=number, head=head, ref=ref, text=text,
                    kept=kept, issue=issue, files=extra[:MAX_FILES])]
    )
    return {"kind": rules.DUPLICATE_CLOSED, "pr": number, "kept": kept, "files": extra,
            "text": text}


def resolve_duplicates(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
) -> list[dict[str, Any]]:
    """Keep one open PR per issue; close the others with their extra files as context."""
    import factory_state_labels as state

    if not store.enabled():
        return []
    by_number = {int(row["number"]): row for row in pulls if row.get("number")}
    groups = state.duplicate_groups([{**row, "headRefName": (row.get("head") or {}).get("ref")}
                                     for row in pulls])
    if not groups:
        return []
    skip = closed_before(github.repo)
    done: list[dict[str, Any]] = []
    resolved = 0
    for issue, numbers in sorted(groups.items()):
        rows = [
            by_number[number] for number in numbers
            if number not in skip and claims_whole(by_number[number], issue)
        ]
        if len(rows) < 2:
            continue
        holders = {int(row["number"]): held(row) for row in rows}
        if any(holders.values()):
            done.append({"issue": issue, "status": "waiting for release",
                         "held": {n: why for n, why in holders.items() if why}})
            continue
        if resolved >= MAX_GROUPS or budget.dry_run or not budget.take():
            done.append({"issue": issue, "status": "would-resolve", "prs": numbers})
            continue
        resolved += 1
        kept, why = keeper(github, rows)
        kept_files = set(files(github, kept))
        for row in rows:
            if int(row["number"]) != kept:
                done.append(close_duplicate(github, store, issue, kept, why, row, kept_files))
    return done
