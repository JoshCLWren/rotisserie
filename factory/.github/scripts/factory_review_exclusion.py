#!/usr/bin/env python3
"""Which models may not review a pull request: the author of its current head.

Owner decision (2026-11-24): only the model or models that authored the current
head are excluded. A model that authored an earlier head may review once a later
commit by someone else is the head. ``factory_review_policy.head_author_record``
holds the order of evidence; this module reads that evidence from GitHub through
one ``fetch`` callable (a repository-relative REST path to decoded JSON), so the
worker's reviewer selection, the outside-review bridge, the review controller and
the operator console resolve a pull request the same way.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

import factory_review_policy as policy
import factory_self_review_allowance as allowance

Fetch = Callable[[str], object]
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
# GitHub's compare API lists at most 300 files; a merge at that size fails closed.
MAX_BASE_MERGE_FILES = 300
MAX_BASE_MERGE_CHAIN = 5


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


CLEAN, NOT_SYNC, UNKNOWN = "clean", "not-sync", "unknown"
FAIL_CLOSED = {
    "unrecorded-worker": "A factory worker pushed the head without a model record",
    "unprovable-merge": "A merge under the head could not be proven to be a clean sync",
    "sync-chain-too-long": f"The head sits on more than {MAX_BASE_MERGE_CHAIN} syncs",
}


def merge_status(
    fetch: Fetch, head: str, *, base_branch: str, commit: object = None
) -> tuple[str, str]:
    """``(status, PR-side parent)``: whether ``head`` only merged the default branch in.

    "Update branch" (or an operator or Rotisserie sync) adds a merge commit
    authored by whoever ran it, so the head loses its attested author although
    it contributes no code of its own. That is provable from GitHub with three
    reads: the head commit (two parents), ``theirs...base`` (the second parent
    is on the default branch), and ``theirs...head``. Because ``theirs`` is an
    ancestor of ``head``, that last diff is exactly what the head adds on top of
    the default branch; ``ours...head`` is what the merge brought in. A file in
    both was changed by the merge relative to the default branch's version: a
    conflict resolution or an extra edit, so ``NOT_SYNC``. A read that fails, or
    a diff too large to list, is ``UNKNOWN``: it neither proves nor disproves.
    """
    payload = _obj(commit if commit is not None else fetch(f"commits/{head}"))
    if str(payload.get("sha") or "") != head:
        return UNKNOWN, ""
    parents = [str(_obj(raw).get("sha") or "") for raw in _rows(payload.get("parents"))]
    if len(parents) != 2:
        return NOT_SYNC, ""
    if not all(HEAD_RE.fullmatch(sha) for sha in parents) or not base_branch:
        return UNKNOWN, ""
    ours, theirs = parents
    on_base = fetch(f"compare/{theirs}...{base_branch}")
    if not isinstance(on_base, dict):
        return UNKNOWN, ""
    if on_base.get("status") not in ("ahead", "identical"):
        return NOT_SYNC, ""

    def changed(diff: object) -> set[str] | None:
        if not isinstance(diff, dict) or not isinstance(diff.get("files"), list):
            return None
        files = diff["files"]
        if len(files) >= MAX_BASE_MERGE_FILES:
            return None
        paths: set[str] = set()
        for raw in files:
            row = _obj(raw)
            if not row.get("filename"):
                return None
            paths.add(str(row["filename"]))
            if row.get("previous_filename"):
                paths.add(str(row["previous_filename"]))
        return paths

    brought_in = changed(fetch(f"compare/{ours}...{head}"))
    beyond_base = changed(fetch(f"compare/{theirs}...{head}"))
    if brought_in is None or beyond_base is None:
        return UNKNOWN, ""
    if brought_in & beyond_base:
        return NOT_SYNC, ""
    return CLEAN, ours


def clean_base_merge_parent(
    fetch: Fetch, head: str, *, base_branch: str, commit: object = None
) -> str | None:
    """The PR-side parent when ``head`` is provably a clean sync, else ``None``."""
    status, parent = merge_status(fetch, head, base_branch=base_branch, commit=commit)
    return parent if status == CLEAN else None


def _safe(fetch: Fetch) -> Fetch:
    def read(path: str) -> object:
        try:
            return fetch(path)
        except (RuntimeError, OSError, ValueError):
            return None

    return read


@dataclass(frozen=True)
class Exclusion:
    """Who may not review one pull request's current head, and why."""

    pr: int
    head: str
    author_commit: str  # the commit whose author record decided; "" when none
    excluded: tuple[str, ...]  # models that may not review the current head
    carried: tuple[str, ...]  # clean default-branch merges walked to reach author_commit
    earlier_authors: tuple[str, ...]  # every recorded author on the PR (informational)
    source: str = ""  # marker, trailer, a FAIL_CLOSED key, or "" (no evidence)
    allowed_by: str = ""  # the person whose allow-self-review label lifted the exclusion
    lifted: tuple[str, ...] = ()  # head authors the owner's exception lets review

    @property
    def via_sync(self) -> bool:
        return bool(self.author_commit) and self.author_commit != self.head

    def matches(self, model: str) -> list[str]:
        """The excluded models that ``model`` is the same actual model as."""
        return [author for author in self.excluded if policy.same_actual_model(model, author)]

    def describe(self) -> str:
        if self.allowed_by:
            return (
                f"{allowance.NOTE} (label by {self.allowed_by}); the head's author"
                f" ({', '.join(self.lifted) or 'none recorded'}) may review this head."
            )
        if not self.author_commit:
            return "The current head has no author record (a human or outside push); none excluded."
        if not self.excluded:
            return "The current head's author is a human or no model is recorded; none excluded."
        names = ", ".join(self.excluded)
        if self.source in FAIL_CLOSED:
            return f"{FAIL_CLOSED[self.source]}; every recorded author ({names}) is excluded."
        if self.via_sync:
            return (
                f"The current head is a sync on {self.author_commit[:10]}, which {names} authored;"
                " that model may not review."
            )
        return f"{names} authored the current head and may not review it."

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "via_sync": self.via_sync}


def resolve(
    fetch: Fetch,
    *,
    pr: int,
    head: str,
    base_branch: str,
    comments: Iterable[str],
    pull: Mapping[str, Any] | None = None,
) -> Exclusion:
    """The current head's excluded authors, lifted by the owner's allow-self-review label.

    ``pull`` carries the labels; without it no exception applies.
    """
    rows = [str(body or "") for body in comments]
    found = _resolve(fetch, pr=pr, head=head, base_branch=base_branch, comments=rows)
    if pull is None:
        return found
    grant = allowance.check(_safe(fetch), pr, pull=pull, head=head, comments=rows)
    if not grant.allowed:
        return found
    return replace(found, excluded=(), allowed_by=grant.actor, lifted=found.excluded)


def _resolve(
    fetch: Fetch,
    *,
    pr: int,
    head: str,
    base_branch: str,
    comments: Iterable[str],
) -> Exclusion:
    """Resolve the current head's author models from controller comment bodies and GitHub.

    A commit whose record names a model decides. A clean sync passes to its
    PR-side parent, even over a ``human`` record (the merge itself adds no code).
    A merge that cannot be proven either way, or more than ``MAX_BASE_MERGE_CHAIN``
    syncs, excludes every recorded author (fail closed). Anything else (a human
    or unrecorded push, a conflict-resolving merge) excludes no model.
    """
    rows = [str(body or "") for body in comments]
    earlier = tuple(policy.known_author_models(rows, pr=pr))
    read = _safe(fetch)
    walked: list[str] = []
    sha = head
    for _ in range(MAX_BASE_MERGE_CHAIN + 1):
        commit = _obj(read(f"commits/{sha}")) if HEAD_RE.fullmatch(sha) else {}
        message = str(_obj(commit.get("commit")).get("message") or "")
        trailers = {sha: policy.model_trailer_models(message)}
        decided, models, source = policy.head_author_record(
            rows, pr=pr, heads=(sha,), trailers=trailers
        )
        if models:
            return Exclusion(pr, head, decided, models, tuple(walked), earlier, source)
        status, parent = merge_status(read, sha, base_branch=base_branch, commit=commit)
        if status == UNKNOWN:
            return Exclusion(pr, head, sha, earlier, tuple(walked), earlier, "unprovable-merge")
        if status == NOT_SYNC:
            return Exclusion(pr, head, decided, models, tuple(walked), earlier, source)
        walked.append(sha)
        sha = parent
    return Exclusion(pr, head, sha, earlier, tuple(walked), earlier, "sync-chain-too-long")


def comment_records(fetch: Fetch, pr: int, *, limit: int = 30) -> list[dict[str, Any]]:
    """Every issue comment on the PR, 100 per read."""
    rows: list[dict[str, Any]] = []
    for page in range(1, limit + 1):
        batch = [
            _obj(row) for row in _rows(fetch(f"issues/{pr}/comments?per_page=100&page={page}"))
        ]
        rows += batch
        if len(batch) < 100:
            break
    return rows


def for_pull(
    fetch: Fetch,
    pr: int,
    *,
    pull: object = None,
    records: Sequence[object] | None = None,
) -> Exclusion:
    """Resolve one pull request's exclusions, reading what the caller did not pass.

    ``records`` are raw issue-comment payloads; only the controller stream is
    trusted for author records, as everywhere else.
    """
    raw = _obj(pull if pull is not None else fetch(f"pulls/{pr}"))
    head = str(_obj(raw.get("head")).get("sha") or raw.get("headRefOid") or "")
    base = str(_obj(raw.get("base")).get("ref") or raw.get("baseRefName") or "")
    rows = list(records) if records is not None else comment_records(fetch, pr)
    bodies = policy.controller_comment_bodies([_obj(row) for row in rows])
    return resolve(fetch, pr=pr, head=head, base_branch=base, comments=bodies, pull=raw)
