#!/usr/bin/env python3
"""The three self-heal actions behind ``factory_self_heal.py sweep``.

Each one reads first, acts at most once per stall, and reports through
``factory_self_heal.report`` so the action is visible on the PR and in the store.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import factory_review_findings as findings
import factory_self_heal as heal
import factory_self_heal_rules as rules
from factory_work_policy_base import is_manual_only

REVIEW = "factory:review"
UNOWNED = "factory:unowned"
MERGE_CONFLICT = "factory:merge-conflict"
THREAD_STAGES = frozenset({REVIEW, "factory:ci", "factory:ready", rules.CHANGES_REQUESTED})
THREADS = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, labels: ["factory"], first: 20, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        headRefOid
        labels(first: 50) { nodes { name } }
        reviewThreads(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            id isResolved isOutdated path
            comments(first: 50) {
              pageInfo { hasNextPage }
              nodes { url body author { login __typename } commit { oid } }
            }
          }
        }
      }
    }
  }
}
"""

RESOLVE = """
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { id isResolved } }
}
"""


@dataclass
class Budget:
    actions: int
    syncs: int
    dry_run: bool = False

    def take(self, kind: str = "action") -> bool:
        if kind == "sync":
            if self.syncs <= 0:
                return False
            self.syncs -= 1
            return True
        if self.actions <= 0:
            return False
        self.actions -= 1
        return True


def _number(row: Mapping[str, Any]) -> int:
    value = row.get("number")
    return value if isinstance(value, int) else 0


def _head(row: Mapping[str, Any]) -> str:
    head = row.get("head")
    return str(head.get("sha") or "") if isinstance(head, Mapping) else ""


def heal_empty_verdicts(
    github: heal.GitHub, store: heal.Store, pulls: Sequence[Mapping[str, Any]], budget: Budget
) -> list[dict[str, Any]]:
    """Move PRs whose repair verdict named no finding back to review, once per verdict."""
    done: list[dict[str, Any]] = []
    for pull in pulls:
        names = heal.labels(pull)
        number = _number(pull)
        if rules.CHANGES_REQUESTED not in names or not number or heal.label_holder(names):
            continue
        found = rules.empty_verdict(github.comments(number), number)
        if found is None or not found.ref or found.healed:
            continue
        holder = store.holder(number)
        if holder:
            done.append({"pr": number, "status": "held", "text": f"#{number}: {holder} holds it"})
            continue
        avoid = found.model if rules.valid_token(found.model) else ""
        model = avoid or "the model that wrote it (no record names it)"
        text = (
            f"Moved #{number} back to review: the {found.verdict} verdict at "
            f"`{found.head[:12]}` named no finding, so repair workers had nothing to fix. "
            f"The next review avoids {model}."
        )
        if budget.dry_run or not budget.take():
            done.append({"pr": number, "status": "would-heal", "text": text})
            continue
        current = github.api(f"issues/{number}")
        current = current if isinstance(current, Mapping) else {}
        live = heal.labels(current)
        if rules.CHANGES_REQUESTED not in live or heal.label_holder(live):
            continue
        if is_manual_only(str(current.get("body") or "")):
            continue
        keep = sorted((live - {rules.CHANGES_REQUESTED}) | {"factory", UNOWNED, REVIEW})
        github.api(f"issues/{number}/labels", "--method", "PUT", payload={"labels": keep})
        done.append(
            heal.report(
                github,
                store,
                kind=rules.EMPTY_VERDICT,
                pr=number,
                head=_head(pull),
                ref=found.ref,
                text=text,
                avoid=avoid,
            )
        )
    return done


def _thread_pages(github: heal.GitHub) -> list[dict[str, Any]]:
    owner, name = github.repo.split("/", 1)
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):
        variables: dict[str, object] = {"owner": owner, "name": name}
        if cursor:
            variables["cursor"] = cursor
        data = github.graphql(THREADS, **variables)
        page = (((data.get("data") or {}).get("repository") or {}).get("pullRequests")) or {}
        found += [node for node in page.get("nodes") or [] if isinstance(node, dict)]
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = str(info.get("endCursor") or "")
    return found


def _contains(github: heal.GitHub, fix: str, head: str) -> bool:
    if head.startswith(fix):
        return True
    compared = github.api(f"compare/{fix}...{head}")
    return isinstance(compared, Mapping) and compared.get("status") in ("ahead", "identical")


def factory_thread_fix(github: heal.GitHub, thread: Mapping[str, Any], pr: int, head: str) -> str:
    """Why the factory's own finding thread is fixed in the head, or "" when it is not."""
    marker = findings.thread_finding(thread, pr=pr)
    if marker is None or marker["head"] == head:
        return ""
    if thread.get("isOutdated"):
        return "GitHub marks its lines outdated in the current head"
    compared = github.api(f"compare/{marker['head']}...{head}")
    files = compared.get("files") if isinstance(compared, Mapping) else None
    touched = isinstance(files, list) and findings.fix_touched(
        files, path=str(thread.get("path") or ""), first=int(marker["start"]), last=int(marker["end"])
    )
    return f"a commit after `{marker['head'][:12]}` changed those lines" if touched else ""


def resolve_answered_threads(
    github: heal.GitHub, store: heal.Store, pulls: Sequence[Mapping[str, Any]], budget: Budget
) -> list[dict[str, Any]]:
    """Resolve bot threads the factory answered "Fixed" with a commit now in the head."""
    del pulls
    done: list[dict[str, Any]] = []
    for node in _thread_pages(github):
        number = _number(node)
        head = str(node.get("headRefOid") or "")
        names = {
            str(label.get("name"))
            for label in ((node.get("labels") or {}).get("nodes") or [])
            if isinstance(label, Mapping)
        }
        listing = node.get("reviewThreads") or {}
        if not number or not head or not names & THREAD_STAGES:
            continue
        for thread in listing.get("nodes") or []:
            page = thread.get("comments") if isinstance(thread, Mapping) else None
            if not isinstance(page, Mapping) or (page.get("pageInfo") or {}).get("hasNextPage"):
                continue
            fix = factory_thread_fix(github, thread, number, head)
            if fix and thread.get("id"):
                text = f"Resolved the factory's finding on {thread.get('path')} in #{number}: {fix}."
                if budget.dry_run or not budget.take():
                    done.append({"pr": number, "status": "would-heal", "text": text})
                    continue
                github.graphql(RESOLVE, thread=str(thread["id"]))
                done.append(heal.report(github, store, kind=rules.THREAD_RESOLVED, pr=number,
                                        head=head, ref=str(thread["id"]), text=text))
                continue
            answered = rules.answered_thread(thread, head)
            if answered is None or not answered.resolvable or not answered.thread_id:
                continue
            if not _contains(github, answered.fix, head):
                done.append(
                    {
                        "pr": number,
                        "status": "fix-not-in-head",
                        "text": f"#{number}: {answered.reviewer}'s thread on {answered.path} "
                        f"names fix {answered.fix[:12]}, which is not in the head; left open",
                    }
                )
                continue
            text = (
                f"Resolved {answered.reviewer}'s thread on {answered.path} in #{number}: "
                f"the factory fixed it in `{answered.fix[:12]}`, which is in the current head."
            )
            if budget.dry_run or not budget.take():
                done.append({"pr": number, "status": "would-heal", "text": text})
                continue
            github.graphql(RESOLVE, thread=answered.thread_id)
            done.append(
                heal.report(
                    github,
                    store,
                    kind=rules.THREAD_RESOLVED,
                    pr=number,
                    head=head,
                    ref=answered.thread_id,
                    text=text,
                )
            )
    return done


def _mergeable(github: heal.GitHub, number: int) -> tuple[bool | None, str, str]:
    live = github.api(f"pulls/{number}")
    if not isinstance(live, Mapping):
        return None, "", ""
    head = live.get("head")
    return (
        live.get("mergeable") if isinstance(live.get("mergeable"), bool) else None,
        str(live.get("mergeable_state") or ""),
        str(head.get("sha") or "") if isinstance(head, Mapping) else "",
    )


def sync_conflicts(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Budget,
    *,
    sync: Any = None,
) -> list[dict[str, Any]]:
    """Merge main into a merge-conflict PR GitHub can now merge, once per head.

    A PR GitHub still reports as conflicting needs code judgment: the attempt
    is recorded and the PR is left to merge-conflict work. Without the store
    there is no once-per-head record, so nothing is attempted.
    """
    import factory_branch_sync as branch_sync

    done: list[dict[str, Any]] = []
    if not store.enabled():
        return done
    for pull in pulls:
        names = heal.labels(pull)
        number = _number(pull)
        if MERGE_CONFLICT not in names or not number or heal.label_holder(names):
            continue
        if budget.syncs <= 0:
            break
        mergeable, state, head = _mergeable(github, number)
        if mergeable is None or not head or store.holder(number):
            continue
        attempt = heal.event(
            kind=rules.CONFLICT_SYNC,
            pr=number,
            head=head,
            ref=head,
            text="attempt",
            mergeable=mergeable,
            mergeable_state=state,
        )
        attempt["event_id"] = f"self-heal:conflict-attempt:{number}:{head}"
        if budget.dry_run:
            done.append({"pr": number, "status": "would-sync" if mergeable else "needs-judgment"})
            continue
        if not store.record([attempt]):
            continue
        if not mergeable:
            done.append(
                {
                    "pr": number,
                    "status": "needs-judgment",
                    "text": f"#{number}: its conflict with main needs code changes; "
                    "left to merge-conflict work",
                }
            )
            continue
        budget.take("sync")
        base = pull.get("base")
        ref = str(base.get("ref") or "") if isinstance(base, Mapping) else ""
        view = branch_sync.PullView(number, "open", head, frozenset(names), ref or "main", "clean")
        outcome = (sync or _sync)(branch_sync, github.repo, view)
        if outcome.status != branch_sync.SYNCED:
            done.append({"pr": number, "status": outcome.status, "text": outcome.text})
            continue
        text = (
            f"Merged {view.base} into #{number}: its conflict with {view.base} no longer needs "
            "code changes, so no model worker was spent on it. Its checks run again."
        )
        done.append(
            heal.report(
                github, store, kind=rules.CONFLICT_SYNC, pr=number, head=head, ref=head, text=text
            )
        )
    return done


def _sync(branch_sync: Any, repo: str, view: Any) -> Any:
    hub = branch_sync.GhHub(repo)
    holder = f"self-heal-{os.environ.get('GITHUB_RUN_ID', 'local')}"
    leases = (
        branch_sync.NeonLeases(repo, holder)
        if os.environ.get("FACTORY_STATE_DSN_FILE")
        else branch_sync.LabelLeases(hub, {view.number: view})
    )
    request = branch_sync.Request(view.number, view.head)
    reader = os.environ.get("GH_TOKEN")
    writer = os.environ.get("FACTORY_SYNC_GH_TOKEN")
    if writer:
        os.environ["GH_TOKEN"] = writer
    try:
        return branch_sync.sync_pull(hub, leases, request, snapshot={view.number: view})
    finally:
        if reader is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = reader
