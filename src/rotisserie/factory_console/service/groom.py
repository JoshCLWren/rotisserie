"""Backlog grooming: inventory every open issue and PR, then apply reviewed corrections.

A backlog drifts from reality: issues stay open after merged work met them, PRs
outlive the issue they implemented, "depends on #N" holds survive N closing,
and infrastructure tickets sit in the adopter instead of upstream. Grooming has
two halves with a person (or an agent working for one) between them:

``inventory`` reads the *entire* open backlog (every page, failing closed on
any page it cannot read) and attaches mechanical evidence to each item:
ownership, linked issues and PRs, declared dependencies and whether they are
still open, merged PRs that reference the issue, likely duplicates, and text
that points at the factory runtime. It proposes a disposition only where the
evidence alone is decisive; everything else is ``active`` or ``unknown``. A
merged PR is evidence to verify, never proof that an issue is done.

``apply`` executes a reviewed plan of typed actions (comment, edit the body,
change labels, close an issue, close a PR, mark a duplicate). Each action
names what its author saw (body hash, PR head, labels) and why. Before every
write the item is read again; the action is refused when the item is closed,
when a worker holds it (or the PR implementing it), when it changed since it
was reviewed, or when the action would close an issue an open PR still
implements. Closing a PR never closes its issue. Each applied action leaves a
marker comment, so applying the same plan twice changes nothing.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import cast

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    integer,
    items,
    label_names,
    obj,
    string,
)

PAGE = 100
MAX_PAGES = 50  # 5,000 items: beyond this the inventory refuses rather than truncates
DUPLICATE_RATIO = 0.86
MARKER = "rotisserie-groom:v1"

DISPOSITIONS = (
    "active",
    "verified-complete",
    "duplicate",
    "superseded",
    "needs-rewrite",
    "wrong-repo",
    "manual-product-decision",
    "unknown",
)

CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)", re.IGNORECASE)
PART_OF = re.compile(r"\b(?:part of|implements|refs?)\s*:?\s+#(\d+)", re.IGNORECASE)
DEPENDS = re.compile(
    r"\b(?:depends on|blocked by|after|requires|waits? (?:on|for))\s*:?\s*#(\d+)",
    re.IGNORECASE,
)
OWNER = re.compile(r"^factory:(\d+|local)$")
# Words that put an issue about the factory runtime itself, not the product.
RUNTIME_WORDS = re.compile(
    r"\b(?:factory (?:worker|scheduler|dispatcher|lease|claim|runtime|controller|"
    r"roster|wakeup|drain|guard|fleet|model catalog|stats)|fixed-model|free-model-factory|"
    r"\.github/(?:scripts|workflows)/(?:factory|fixed-model|free-model))",
    re.IGNORECASE,
)


class InventoryError(RuntimeError):
    """The full backlog could not be read; nothing partial is reported as complete."""


@dataclass(frozen=True)
class Signal:
    code: str
    text: str
    refs: tuple[int, ...] = ()


@dataclass
class Item:
    kind: str  # issue | pr
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    url: str
    updated_at: str
    created_at: str
    owner: str = ""  # factory owner label naming a worker, if any
    head: str = ""  # PR head SHA
    linked: tuple[int, ...] = ()  # issues a PR closes or is part of
    implemented_by: tuple[int, ...] = ()  # open PRs that close or are part of this issue
    depends_on: tuple[int, ...] = ()
    disposition: str = "active"
    signals: list[Signal] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.number}"

    @property
    def body_sha256(self) -> str:
        return body_hash(self.body)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("body")
        data["ref"] = self.ref
        data["body_sha256"] = self.body_sha256
        return data


@dataclass(frozen=True)
class Inventory:
    issues: tuple[Item, ...]
    prs: tuple[Item, ...]

    @property
    def counts(self) -> dict[str, dict[str, int]]:
        return {
            "issue": dict(Counter(item.disposition for item in self.issues)),
            "pr": dict(Counter(item.disposition for item in self.prs)),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "totals": {"issues": len(self.issues), "prs": len(self.prs)},
            "counts": self.counts,
            "issues": [item.to_dict() for item in self.issues],
            "prs": [item.to_dict() for item in self.prs],
        }


def body_hash(body: str) -> str:
    return hashlib.sha256(body.replace("\r\n", "\n").encode()).hexdigest()


def manual_only(body: str) -> bool:
    """The factory's own structured manual-only directive (not a passing mention)."""
    try:
        from rotisserie.factory_console import runtime

        return bool(runtime.module("factory_work_policy").is_manual_only(body))
    except Exception:  # noqa: BLE001 - runtime unavailable: fall back to a line-start rule
        return bool(re.search(r"(?im)^\s*(?:\*\*)?manual[- ]only\b", body or ""))


def runtime_owner(labels: Iterable[str]) -> str:
    return next((label for label in sorted(labels) if OWNER.match(label)), "")


def _all_pages(github: GitHub, path: str) -> list[Obj]:
    """Every page of a list endpoint; raises rather than return a partial list."""
    found: list[Obj] = []
    sep = "&" if "?" in path else "?"
    for page in range(1, MAX_PAGES + 1):
        try:
            rows = items(github.repo_api(f"{path}{sep}per_page={PAGE}&page={page}"))
        except GitHubError as exc:
            raise InventoryError(f"could not read page {page} of {path}: {exc}") from exc
        found.extend(rows)
        if len(rows) < PAGE:
            return found
    raise InventoryError(f"{path} has more than {MAX_PAGES * PAGE} entries; refusing to truncate")


def _numbers(pattern: re.Pattern[str], text: str, *, not_self: int) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(n) for n in pattern.findall(text or "") if int(n) != not_self))


def read_backlog(github: GitHub) -> tuple[list[Item], list[Item]]:
    """Every open issue and PR (the issues endpoint lists both)."""
    rows = _all_pages(github, "issues?state=open&sort=created&direction=asc")
    issues: list[Item] = []
    prs: list[Item] = []
    for row in rows:
        number = integer(row, "number")
        if number is None:
            raise InventoryError("GitHub returned an item without a number")
        labels = tuple(sorted(label_names(row)))
        body = string(row, "body")
        item = Item(
            "pr" if "pull_request" in row else "issue",
            number,
            string(row, "title").replace("\n", " "),
            body,
            labels,
            string(row, "html_url"),
            string(row, "updated_at"),
            string(row, "created_at"),
            owner=runtime_owner(labels),
        )
        (prs if item.kind == "pr" else issues).append(item)
    return issues, prs


def _pr_details(github: GitHub, pr: Item) -> None:
    data = obj(github.repo_api(f"pulls/{pr.number}"))
    pr.head = string(obj(data.get("head")), "sha")
    branch = string(obj(data.get("head")), "ref")
    text = f"{pr.title}\n{pr.body}"
    linked = list(_numbers(CLOSING, text, not_self=pr.number))
    linked += [n for n in _numbers(PART_OF, text, not_self=pr.number) if n not in linked]
    match = re.search(r"issue-(\d+)", branch)
    if match and int(match.group(1)) not in linked:
        linked.append(int(match.group(1)))
    pr.linked = tuple(linked)


def _merged_references(github: GitHub, issue: int) -> tuple[int, ...]:
    """Merged PRs that say they close this issue or are part of it.

    A bare mention ("see #N") is not counted: only a closing keyword or a
    "part of" link claims the PR delivered some of the issue.
    """
    merged = []
    for event in _all_pages(github, f"issues/{issue}/timeline"):
        if string(event, "event") != "cross-referenced":
            continue
        source = obj(obj(event.get("source")).get("issue"))
        pull = obj(source.get("pull_request"))
        if not pull or not string(pull, "merged_at"):
            continue
        text = f"{string(source, 'title')}\n{string(source, 'body')}"
        claims = set(CLOSING.findall(text)) | set(PART_OF.findall(text))
        number = integer(source, "number")
        if number is not None and str(issue) in claims:
            merged.append(number)
    return tuple(dict.fromkeys(merged))


def _state(github: GitHub, number: int, cache: dict[int, str]) -> str:
    if number not in cache:
        try:
            cache[number] = string(obj(github.repo_api(f"issues/{number}")), "state") or "?"
        except GitHubError:
            cache[number] = "?"
    return cache[number]


def similar_pairs(
    issues: list[Item], ratio: float = DUPLICATE_RATIO
) -> list[tuple[int, int, float]]:
    """Open issues whose titles are near-identical (likely duplicates to verify)."""
    norm = {item.number: re.sub(r"[^a-z0-9 ]+", " ", item.title.lower()).split() for item in issues}
    found = []
    ordered = sorted(issues, key=lambda item: item.number)
    for index, first in enumerate(ordered):
        for second in ordered[index + 1 :]:
            a, b = norm[first.number], norm[second.number]
            if not a or not b or abs(len(a) - len(b)) > max(len(a), len(b)) // 2:
                continue
            score = difflib.SequenceMatcher(None, a, b).ratio()
            if score >= ratio:
                found.append((first.number, second.number, round(score, 2)))
    return found


def inventory(github: GitHub, *, timelines: bool = True) -> Inventory:
    """Read the whole open backlog and attach evidence to each item."""
    issues, prs = read_backlog(github)
    open_numbers = {item.number for item in issues} | {item.number for item in prs}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda pr: _pr_details(github, pr), prs))
    implementing: dict[int, list[int]] = {}
    for pr in prs:
        for issue in pr.linked:
            implementing.setdefault(issue, []).append(pr.number)
    states: dict[int, str] = {}
    merged: dict[int, tuple[int, ...]] = {}
    if timelines:
        with ThreadPoolExecutor(max_workers=8) as pool:
            merged = dict(
                zip(
                    (item.number for item in issues),
                    pool.map(lambda item: _merged_references(github, item.number), issues),
                    strict=True,
                )
            )
    by_number = {item.number: item for item in issues}
    for item in issues:
        item.implemented_by = tuple(implementing.get(item.number, ()))
        item.depends_on = _numbers(DEPENDS, item.body, not_self=item.number)
        _classify_issue(item, github, open_numbers, states, merged.get(item.number, ()))
    for first, second, score in similar_pairs(issues):
        for one, other in ((first, second), (second, first)):
            by_number[one].signals.append(
                Signal("similar-title", f"title is {score:.0%} like #{other}", (other,))
            )
    for pr in prs:
        _classify_pr(pr, github, by_number, states)
    return Inventory(tuple(issues), tuple(prs))


def _classify_issue(
    item: Item,
    github: GitHub,
    open_numbers: set[int],
    states: dict[int, str],
    merged: tuple[int, ...],
) -> None:
    text = f"{item.title}\n{item.body}"
    if item.owner:
        item.signals.append(Signal("held", f"{item.owner} holds it"))
    if item.implemented_by:
        refs = item.implemented_by
        item.signals.append(
            Signal(
                "open-pr", "open PR " + ", ".join(f"#{n}" for n in refs) + " implements it", refs
            )
        )
    if merged:
        item.signals.append(
            Signal(
                "merged-pr",
                "merged PR "
                + ", ".join(f"#{n}" for n in merged)
                + " references it (verify the acceptance criteria; a merge alone is not proof)",
                merged,
            )
        )
    closed_deps = [
        n
        for n in item.depends_on
        if n not in open_numbers and _state(github, n, states) == "closed"
    ]
    if closed_deps:
        item.signals.append(
            Signal(
                "closed-dependency",
                "declares a dependency on closed "
                + ", ".join(f"#{n}" for n in closed_deps)
                + "; the hold may be stale",
                tuple(closed_deps),
            )
        )
    if "blocked" in item.labels and item.depends_on and len(closed_deps) == len(item.depends_on):
        item.signals.append(
            Signal("stale-block", "labeled blocked, but every declared dependency is closed")
        )
    if manual_only(item.body) or "manual-only" in item.labels:
        item.disposition = "manual-product-decision"
    if RUNTIME_WORDS.search(text):
        item.signals.append(
            Signal("runtime-text", "it describes the factory runtime, which lives upstream")
        )
        if item.disposition == "active":
            item.disposition = "unknown"
    if merged and item.disposition == "active" and not item.implemented_by:
        item.disposition = "unknown"  # needs verification, not closure


def _classify_pr(pr: Item, github: GitHub, issues: dict[int, Item], states: dict[int, str]) -> None:
    if pr.owner:
        pr.signals.append(Signal("held", f"{pr.owner} holds it"))
    if not pr.linked:
        pr.signals.append(Signal("no-issue", "it names no issue it implements"))
        return
    closed = [n for n in pr.linked if n not in issues and _state(github, n, states) == "closed"]
    if closed:
        pr.signals.append(
            Signal(
                "issue-closed",
                "its issue " + ", ".join(f"#{n}" for n in closed) + " is already closed",
                tuple(closed),
            )
        )
        if len(closed) == len(pr.linked) and not pr.owner:
            pr.disposition = "superseded"


# Applying a reviewed plan -------------------------------------------------------------

# Which targets each operation may act on. Closing a PR never closes its issue.
KINDS = {
    "comment": ("issue", "pr"),
    "edit-body": ("issue", "pr"),
    "labels": ("issue", "pr"),
    "close-issue": ("issue",),
    "duplicate": ("issue",),
    "close-pr": ("pr",),
    "transfer": ("issue",),
}
OPS = tuple(KINDS)
REASONS = ("completed", "not_planned")


@dataclass(frozen=True)
class Action:
    id: str
    target: str  # issue:N | pr:N
    op: str
    why: str
    comment: str = ""
    body: str = ""
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    reason: str = ""  # close-issue: completed | not_planned
    duplicate_of: int | None = None
    to_repo: str = ""  # transfer: owner/name of the repository that should own the issue
    expect_body_sha256: str = ""
    expect_head: str = ""
    expect_labels: tuple[str, ...] | None = None

    @property
    def kind(self) -> str:
        return self.target.split(":", 1)[0]

    @property
    def number(self) -> int:
        return int(self.target.split(":", 1)[1])


@dataclass(frozen=True)
class Applied:
    id: str
    target: str
    op: str
    status: str  # applied | would-apply | skipped | refused | failed
    text: str


class PlanError(ValueError):
    pass


def parse_plan(raw: str) -> list[Action]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PlanError(f"the plan is not JSON: {exc}") from exc
    rows = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise PlanError('the plan needs a non-empty "actions" list')
    actions: list[Action] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PlanError(f"action {index} is not an object")
        row = cast(dict[str, object], row)
        action = _action(row, index)
        if action.id in seen:
            raise PlanError(f"action id {action.id!r} appears twice")
        seen.add(action.id)
        actions.append(action)
    return actions


def _text(row: dict[str, object], key: str) -> str:
    value = row.get(key, "")
    if not isinstance(value, str):
        raise PlanError(f"{key} must be text")
    return value


def _strings(row: dict[str, object], key: str) -> tuple[str, ...]:
    value = row.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PlanError(f"{key} must be a list of text")
    return tuple(cast(list[str], value))


def _action(row: dict[str, object], index: int) -> Action:
    op = _text(row, "op")
    target = _text(row, "target")
    if op not in OPS:
        raise PlanError(f"action {index}: op must be one of {', '.join(OPS)}")
    if not re.fullmatch(r"(issue|pr):\d+", target):
        raise PlanError(f"action {index}: target must look like issue:N or pr:N")
    why = _text(row, "why").strip()
    if not why:
        raise PlanError(f"action {index}: every action needs a why (its evidence)")
    expect = row.get("expect", {})
    if not isinstance(expect, dict):
        raise PlanError(f"action {index}: expect must be an object")
    expect = cast(dict[str, object], expect)
    labels = expect.get("labels")
    if labels is not None and (
        not isinstance(labels, list) or not all(isinstance(v, str) for v in labels)
    ):
        raise PlanError(f"action {index}: expect.labels must be a list of text")
    duplicate_of = row.get("duplicate_of")
    if duplicate_of is not None and not isinstance(duplicate_of, int):
        raise PlanError(f"action {index}: duplicate_of must be an issue number")
    action = Action(
        id=_text(row, "id") or f"{op}-{target.replace(':', '-')}",
        target=target,
        op=op,
        why=why,
        comment=_text(row, "comment"),
        body=_text(row, "body"),
        add=_strings(row, "add"),
        remove=_strings(row, "remove"),
        reason=_text(row, "reason"),
        duplicate_of=duplicate_of,
        to_repo=_text(row, "to_repo"),
        expect_body_sha256=str(expect.get("body_sha256", "") or ""),
        expect_head=str(expect.get("head", "") or ""),
        expect_labels=tuple(sorted(cast(list[str], labels))) if labels is not None else None,
    )
    _validate(action, index)
    return action


def _validate(action: Action, index: int) -> None:
    where = f"action {index} ({action.id})"
    if action.kind not in KINDS[action.op]:
        raise PlanError(f"{where}: {action.op} applies to {' or '.join(KINDS[action.op])}")
    if action.op == "close-issue" and action.reason not in REASONS:
        raise PlanError(f"{where}: close-issue needs reason completed or not_planned")
    if action.op in ("close-issue", "close-pr", "duplicate", "comment", "transfer") and not (
        action.comment
    ):
        raise PlanError(f"{where}: {action.op} needs a comment explaining it")
    if action.op == "duplicate" and not action.duplicate_of:
        raise PlanError(f"{where}: duplicate needs duplicate_of")
    if action.op == "edit-body":
        if not action.body.strip():
            raise PlanError(f"{where}: edit-body needs the new body")
        if not action.expect_body_sha256:
            raise PlanError(f"{where}: edit-body needs expect.body_sha256 (the body you reviewed)")
    if action.op == "transfer" and not re.fullmatch(r"[\w.-]+/[\w.-]+", action.to_repo):
        raise PlanError(f"{where}: transfer needs to_repo as owner/name")
    if action.op == "labels" and not (action.add or action.remove):
        raise PlanError(f"{where}: labels needs add or remove")
    if action.op == "close-pr" and not action.expect_head:
        raise PlanError(f"{where}: close-pr needs expect.head (the head you reviewed)")


def marker(action: Action) -> str:
    return f"<!-- {MARKER}:{action.id} -->"


def _with_marker(action: Action, text: str) -> str:
    return f"{text.rstrip()}\n\n{marker(action)}"


def _already_applied(github: GitHub, action: Action) -> bool:
    if marker(action) in string(obj(github.repo_api(f"issues/{action.number}")), "body"):
        return True  # recorded in the body because the issue does not accept comments
    comments = _all_pages(github, f"issues/{action.number}/comments")
    return any(marker(action) in string(row, "body") for row in comments)


def _note(github: GitHub, action: Action, text: str) -> None:
    """Post the action's explanation with its marker.

    GitHub refuses comments on issues with very many comments (a long-lived
    registry, say). The explanation must still be on record, and the marker must
    still make the action idempotent, so it goes at the top of the body instead.
    """
    try:
        github.repo_api(f"issues/{action.number}/comments", method="POST",
                        fields={"body": _with_marker(action, text)})  # fmt: skip
    except GitHubError as exc:
        if "commenting is disabled" not in str(exc).lower():
            raise
        body = string(obj(github.repo_api(f"issues/{action.number}")), "body")
        note = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
        github.repo_api(f"issues/{action.number}", method="PATCH",
                        fields={"body": f"{note}\n\n{marker(action)}\n\n{body}"})  # fmt: skip


def check(github: GitHub, action: Action) -> tuple[str, str, Obj]:
    """Re-read the target; (status, reason, live) where status is ok | skipped | refused."""
    live = obj(github.repo_api(f"issues/{action.number}"))
    is_pr = "pull_request" in live
    if (action.kind == "pr") != is_pr:
        return "refused", f"#{action.number} is not a{'' if is_pr else 'n'} {action.kind}", live
    if string(live, "state") != "open":
        return "skipped", f"#{action.number} is already closed", live
    labels = label_names(live)
    holder = runtime_owner(labels)
    if holder and action.op != "comment":
        return "refused", f"{holder} holds #{action.number}; defer until its worker finishes", live
    if action.expect_body_sha256 and body_hash(string(live, "body")) != action.expect_body_sha256:
        return "refused", f"#{action.number}'s body changed since it was reviewed", live
    if action.expect_labels is not None and tuple(sorted(labels)) != action.expect_labels:
        return "refused", f"#{action.number}'s labels changed since it was reviewed", live
    if action.kind == "pr":
        head = string(obj(obj(github.repo_api(f"pulls/{action.number}")).get("head")), "sha")
        if action.expect_head and head != action.expect_head:
            return "refused", f"PR #{action.number}'s head moved since it was reviewed", live
    if action.op in ("close-issue", "duplicate", "transfer"):
        blockers = _open_implementing_prs(github, action.number)
        if blockers:
            names = ", ".join(f"#{n}" for n in blockers)
            return (
                "refused",
                f"open PR {names} still implements #{action.number}; decide the PR first",
                live,
            )
    return "ok", "", live


def _open_implementing_prs(github: GitHub, issue: int) -> list[int]:
    found = []
    for event in _all_pages(github, f"issues/{issue}/timeline"):
        if string(event, "event") != "cross-referenced":
            continue
        source = obj(obj(event.get("source")).get("issue"))
        if "pull_request" not in source or string(source, "state") != "open":
            continue
        text = f"{string(source, 'title')}\n{string(source, 'body')}"
        number = integer(source, "number")
        refs = set(CLOSING.findall(text)) | set(PART_OF.findall(text))
        if number is not None and str(issue) in refs:
            found.append(number)
    return sorted(set(found))


def _write(github: GitHub, action: Action) -> None:
    n = action.number
    if action.op == "comment":
        _note(github, action, action.comment)
        return
    if action.op == "edit-body":
        github.repo_api(f"issues/{n}", method="PATCH", fields={"body": action.body})
        _note(
            github,
            action,
            action.comment or f"Description updated by backlog grooming: {action.why}",
        )
        return
    if action.op == "labels":
        for label in action.add:
            github.repo_api(f"issues/{n}/labels", method="POST", fields={"labels[]": label})
        for label in action.remove:
            try:
                github.repo_api(f"issues/{n}/labels/{label}", method="DELETE")
            except GitHubError as exc:
                if "does not exist" not in str(exc).lower():
                    raise
        _note(github, action, action.comment or f"Labels updated by backlog grooming: {action.why}")
        return
    # Closures: comment first, so the reason is on record even if the close fails.
    text = action.comment
    if action.op == "duplicate":
        text = f"Duplicate of #{action.duplicate_of}.\n\n{text}"
    _note(github, action, text)
    if action.op == "close-pr":
        github.repo_api(f"pulls/{n}", method="PATCH", fields={"state": "closed"})
        return
    if action.op == "transfer":
        # The issue moves with its comments (including the marker); GitHub redirects
        # the old number, so a second run finds the marker and skips.
        github.gh("issue", "transfer", str(n), action.to_repo, "--repo", github.repo)
        return
    reason = "not_planned" if action.op == "duplicate" else action.reason
    github.repo_api(f"issues/{n}", method="PATCH",
                    fields={"state": "closed", "state_reason": reason})  # fmt: skip


def apply(
    github: GitHub,
    actions: list[Action],
    *,
    write: bool,
    record: Callable[[Applied], None] | None = None,
) -> list[Applied]:
    """Check (and with ``write``, perform) each action against fresh live state."""
    done: list[Applied] = []
    for action in actions:
        try:
            if _already_applied(github, action):
                result = Applied(action.id, action.target, action.op, "skipped",
                                 "already applied (its marker comment exists)")  # fmt: skip
            else:
                status, reason, _live = check(github, action)
                if status != "ok":
                    result = Applied(action.id, action.target, action.op, status, reason)
                elif not write:
                    result = Applied(action.id, action.target, action.op, "would-apply",
                                     action.why)  # fmt: skip
                else:
                    _write(github, action)
                    result = Applied(action.id, action.target, action.op, "applied", action.why)
        except (GitHubError, InventoryError) as exc:
            result = Applied(action.id, action.target, action.op, "failed", str(exc))
        done.append(result)
        if record is not None:
            record(result)
    return done
