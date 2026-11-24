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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import factory_review_policy as policy

Fetch = Callable[[str], object]
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
# GitHub's compare API lists at most 300 files; a merge at that size fails closed.
MAX_BASE_MERGE_FILES = 300
MAX_BASE_MERGE_CHAIN = 5


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def clean_base_merge_parent(
    fetch: Fetch, head: str, *, base_branch: str, commit: object = None
) -> str | None:
    """The PR-side parent when ``head`` only merged the default branch in, else ``None``.

    "Update branch" (or an operator or Rotisserie sync) adds a merge commit
    authored by whoever ran it, so the head loses its attested author although
    it contributes no code of its own. That is provable from GitHub with three
    reads: the head commit (two parents), ``theirs...base`` (the second parent
    is on the default branch), and ``theirs...head``. Because ``theirs`` is an
    ancestor of ``head``, that last diff is exactly what the head adds on top of
    the default branch; ``ours...head`` is what the merge brought in. A file in
    both was changed by the merge relative to the default branch's version: a
    conflict resolution or an extra edit. Any such overlap fails closed.
    """
    payload = _obj(commit if commit is not None else fetch(f"commits/{head}"))
    if str(payload.get("sha") or "") != head:
        return None
    parents = [str(_obj(raw).get("sha") or "") for raw in _rows(payload.get("parents"))]
    if len(parents) != 2 or not all(HEAD_RE.fullmatch(sha) for sha in parents):
        return None
    ours, theirs = parents
    if not base_branch:
        return None
    on_base = fetch(f"compare/{theirs}...{base_branch}")
    if not isinstance(on_base, dict) or on_base.get("status") not in ("ahead", "identical"):
        return None

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
    if brought_in is None or beyond_base is None or brought_in & beyond_base:
        return None
    return ours


@dataclass(frozen=True)
class Exclusion:
    """Who may not review one pull request's current head, and why."""

    pr: int
    head: str
    author_commit: str  # the commit whose author record decided; "" when none
    excluded: tuple[str, ...]  # models that may not review the current head
    carried: tuple[str, ...]  # clean default-branch merges walked to reach author_commit
    earlier_authors: tuple[str, ...]  # every recorded author on the PR (informational)
    source: str = ""  # marker | trailer | unrecorded-worker | "" (no evidence)

    @property
    def via_sync(self) -> bool:
        return bool(self.author_commit) and self.author_commit != self.head

    def matches(self, model: str) -> list[str]:
        """The excluded models that ``model`` is the same actual model as."""
        return [author for author in self.excluded if policy.same_actual_model(model, author)]

    def describe(self) -> str:
        if not self.author_commit:
            return "The current head has no author record (a human push or a sync); none excluded."
        if not self.excluded:
            return "The current head's author record names no model (a human); none excluded."
        names = ", ".join(self.excluded)
        if self.source == "unrecorded-worker":
            return (
                f"A factory worker pushed {self.author_commit[:10]} without a model record;"
                f" every recorded author ({names}) is excluded."
            )
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
) -> Exclusion:
    """Resolve the current head's author models from controller comment bodies and GitHub.

    Each commit is read once for its ``Model:`` trailers and parents. A commit
    without author evidence passes to its PR-side parent only when it is a clean
    default-branch merge, up to ``MAX_BASE_MERGE_CHAIN`` merges.
    """
    rows = [str(body or "") for body in comments]
    earlier = tuple(policy.known_author_models(rows, pr=pr))
    walked: list[str] = []
    sha = head
    for _ in range(MAX_BASE_MERGE_CHAIN + 1):
        commit = _obj(fetch(f"commits/{sha}")) if HEAD_RE.fullmatch(sha) else {}
        message = str(_obj(commit.get("commit")).get("message") or "")
        trailers = {sha: policy.model_trailer_models(message)}
        decided, models, source = policy.head_author_record(
            rows, pr=pr, heads=(sha,), trailers=trailers
        )
        if decided:
            return Exclusion(pr, head, decided, models, tuple(walked), earlier, source)
        parent = clean_base_merge_parent(fetch, sha, base_branch=base_branch, commit=commit)
        if parent is None:
            break
        walked.append(sha)
        sha = parent
    return Exclusion(pr, head, "", (), (), earlier)


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
    return resolve(fetch, pr=pr, head=head, base_branch=base, comments=bodies)
