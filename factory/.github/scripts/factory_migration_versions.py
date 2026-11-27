#!/usr/bin/env python3
"""Reject a PR whose new migration versions collide with the base branch or with each other.

Two PRs can each add ``V9__x.sql`` while main has only ``V8``. The migration lane
serializes them, but after the first merges the second still carries ``V9``. This
check runs in the exact-head merge gate against the current base branch, so the
second PR goes back to repair with the reason instead of merging a duplicate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import factory_migration_families as migration_families

CONTENTS_LIMIT = 1000

Fetch = Callable[[list[str]], object]


def base_versions(family: migration_families.Family, names: Iterable[str]) -> dict[int, str]:
    """The versions already on the base branch for one family, keyed to their file path."""
    found: dict[int, str] = {}
    for name in names:
        match = family.version.fullmatch(name) if family.version else None
        digits = match.group("version") if match else ""
        if digits.isdigit():
            found.setdefault(int(digits), family.directory + name)
    return found


def conflicts(
    changes: Iterable[migration_families.Change],
    declared: Iterable[migration_families.Family],
    base: Mapping[str, Mapping[int, str]],
) -> list[str]:
    """Each reason the PR's migration changes cannot merge onto the base branch."""
    by_name = {family.name: family for family in declared}
    reasons: list[str] = []
    seen: dict[tuple[str, int], str] = {}
    for change in changes:
        family = by_name[change.family]
        if family.version is None:
            continue
        if change.ambiguous:
            reasons.append(
                f"{change.path} ({change.status or 'changed'}) is not a new {family.name} "
                f"migration whose name matches {family.version.pattern}; add a new file instead"
            )
            continue
        if change.version is None:
            continue
        key = (family.name, change.version)
        if key in seen:
            reasons.append(f"{change.path} and {seen[key]} both use version {change.version}")
            continue
        seen[key] = change.path
        existing = base.get(family.name, {})
        top = max(existing, default=0)
        if change.version in existing and existing[change.version] != change.path:
            reasons.append(
                f"{change.path} uses version {change.version}, which the base branch already has "
                f"as {existing[change.version]}; renumber it to {top + 1}"
            )
        elif family.increasing and change.version <= top and change.version not in existing:
            reasons.append(
                f"{change.path} uses version {change.version}, but the base branch is already at "
                f"{top}; renumber it to {top + 1}"
            )
    return reasons


def _listing(fetch: Fetch, repo: str, directory: str, ref: str) -> list[str] | None:
    """File names in a base-branch directory; None when the listing may be cut short."""
    try:
        rows = fetch(["api", f"repos/{repo}/contents/{directory.rstrip('/')}?ref={ref}"])
    except RuntimeError as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            return []
        raise
    if not isinstance(rows, list) or len(rows) >= CONTENTS_LIMIT:
        return None
    return [str(row.get("name") or "") for row in rows if isinstance(row, dict)]


def _rows(pages: object) -> list[dict[str, Any]]:
    flat = (
        [row for page in pages for row in (page if isinstance(page, list) else [page])]
        if isinstance(pages, list)
        else []
    )
    return [row for row in flat if isinstance(row, dict)]


def gate(repo: str, pr_number: int, expected_head: str, fetch: Fetch) -> tuple[str, str]:
    """(decision, reason) for the exact head: pass, retry, or deny with every collision."""
    declared = migration_families.families()
    if all(family.version is None for family in declared):
        return "pass", "no migration family has ordered versions"
    pr = fetch(["api", f"repos/{repo}/pulls/{pr_number}"])
    if not isinstance(pr, dict):
        return "retry", "pull request could not be read for the migration version check"
    head = str((pr.get("head") or {}).get("sha") or "")
    if head != expected_head:
        return "retry", f"head moved to {head[:12]} during the migration version check"
    ref = str((pr.get("base") or {}).get("ref") or "")
    files = f"repos/{repo}/pulls/{pr_number}/files?per_page=100"
    rows = _rows(fetch(["api", "--paginate", "--slurp", files]))
    changes = [change for row in rows for change in migration_families.changes(row, declared)]
    touched = {change.family for change in changes}
    base: dict[str, dict[int, str]] = {}
    for family in declared:
        if family.version is None or family.name not in touched:
            continue
        names = _listing(fetch, repo, family.directory, ref)
        if names is None:
            return "retry", f"{family.directory} on {ref} could not be listed in full"
        base[family.name] = base_versions(family, names)
    reasons = conflicts(changes, declared, base)
    if reasons:
        return "deny", "migration version conflict: " + "; ".join(reasons)
    return "pass", "migration versions are new and unique on the base branch"


def merge_gate(repo: str, pr_number: int, expected_head: str, fetch: Fetch) -> dict[str, str]:
    """The merge gate's view: an unreadable state or invalid config holds the merge."""
    try:
        decision, reason = gate(repo, pr_number, expected_head, fetch)
    except migration_families.AdopterConfigError as exc:
        decision, reason = "retry", f"migration family config is invalid: {exc}"
    except RuntimeError as exc:
        decision, reason = "retry", f"migration version check could not read GitHub: {exc}"
    return {"decision": decision, "reason": reason}
