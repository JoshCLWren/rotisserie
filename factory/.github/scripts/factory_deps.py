"""Dependency links between factory items, after Beads' ``bd dep``.

Idea credit: Beads (MIT, github.com/gastownhall/beads); reimplemented here.

A link is ``(src, dst, kind)`` on issue or PR numbers of one repository:

- ``blocks``: ``dst`` waits until ``src`` closes.
- ``parent-child``: ``src`` is the parent and ``dst`` the child. The parent
  is a tracking item: it waits until every child closes. A child inherits
  each open blocker of its ancestors, except the ancestors' own descendants,
  so a parent never blocks its own children or their siblings. Auto-split
  writes one ``parent-child`` link per child and ``blocks`` links between
  children that must run in order.
- ``related``, ``duplicates`` (``src`` duplicates ``dst``) and ``supersedes``
  (``src`` replaces ``dst``) are annotations and never hold work.

Links come from two places. ``Blocked by #N`` and ``Depends on #N`` lines in
open issue bodies are reconciled into the store once per dispatcher pass
(origin ``body``). The factory writes its own links with ``add`` (origin
``factory``); a body reconcile never deletes those. A factory write that would
close a direct cycle is refused. A cycle declared in issue bodies, or one
that only appears through a child inheriting its parent's blockers, cannot be
refused at write time, so ``Graph.cycles`` reports it and its members stay
held until a person removes one link.

Public API, for other factory code::

    factory_deps.add(repo, src, dst, kind)      # "added" or "exists"; DependencyCycle
    factory_deps.remove(repo, src, dst, kind)   # True when a row was deleted
    factory_deps.read_links(repo)               # every stored Link
    factory_deps.blockers(repo, item, open_numbers=...)  # open blocker numbers
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from factory_work_policy_base import eligibility

KINDS = ("blocks", "parent-child", "related", "duplicates", "supersedes")
GATING = frozenset({"blocks", "parent-child"})
ORIGINS = ("body", "factory")
MAX_LINKS = 5000
MAX_RECONCILE_WRITES = 200


class DependencyCycle(ValueError):
    """The link would make an item wait on itself."""


@dataclass(frozen=True, order=True)
class Link:
    src: int
    dst: int
    kind: str
    origin: str = "factory"

    def to_dict(self) -> dict[str, object]:
        return {"src": self.src, "dst": self.dst, "kind": self.kind, "origin": self.origin}


def item_number(value: int | str) -> int:
    """``7``, ``"7"``, ``"#7"``, ``"issue:7"`` or ``"pr:7"`` as a positive number."""
    text = str(value).strip().removeprefix("#")
    for prefix in ("issue:", "pr:"):
        text = text.removeprefix(prefix)
    if not text.isascii() or not text.isdecimal() or int(text) <= 0:
        raise ValueError(f"not an issue or PR reference: {value!r}")
    return int(text)


def link(src: int | str, dst: int | str, kind: str, origin: str = "factory") -> Link:
    if kind not in KINDS:
        raise ValueError(f"unknown dependency kind {kind!r}; use one of {', '.join(KINDS)}")
    if origin not in ORIGINS:
        raise ValueError(f"unknown dependency origin {origin!r}")
    found = Link(item_number(src), item_number(dst), kind, origin)
    if found.src == found.dst:
        raise DependencyCycle(f"#{found.src} cannot depend on itself")
    return found


def body_links(number: int, body: str | None) -> set[Link]:
    """The ``blocks`` links an issue body declares on itself (anchored forms only)."""
    return {
        Link(blocker, number, "blocks", "body")
        for blocker in eligibility.parse_declared_dependencies(body)
        if blocker != number
    }


def issue_body_links(issues: Iterable[Mapping[str, Any]]) -> set[Link]:
    found: set[Link] = set()
    for issue in issues:
        found |= body_links(int(issue["number"]), str(issue.get("body") or ""))
    return found


class Graph:
    """Gating links over the currently open items; pure, no store or GitHub reads."""

    def __init__(self, links: Iterable[Link], open_numbers: Collection[int]) -> None:
        self.open = frozenset(open_numbers)
        self.links = tuple(sorted(set(links)))
        self._blocked_by: dict[int, set[int]] = {}
        self._parents: dict[int, set[int]] = {}
        self._children: dict[int, set[int]] = {}
        for found in self.links:
            if found.kind == "blocks":
                self._blocked_by.setdefault(found.dst, set()).add(found.src)
            elif found.kind == "parent-child":
                self._parents.setdefault(found.dst, set()).add(found.src)
                self._children.setdefault(found.src, set()).add(found.dst)
        self._memo: dict[int, tuple[int, ...]] = {}
        self._cycles = self._find_cycles()
        self._in_cycle = {number: cycle for cycle in self._cycles for number in cycle}

    def _descendants(self, item: int) -> set[int]:
        found: set[int] = set()
        pending = list(self._children.get(item, ()))
        while pending:
            child = pending.pop()
            if child not in found:
                found.add(child)
                pending.extend(self._children.get(child, ()))
        return found

    def blockers(self, item: int) -> tuple[int, ...]:
        """Open items ``item`` waits on: its blockers, its open children, its ancestors' blockers."""
        if item in self._memo:
            return self._memo[item]
        found = {n for n in self._blocked_by.get(item, ()) if n in self.open}
        found |= {n for n in self._children.get(item, ()) if n in self.open}
        seen: set[int] = set()
        pending = list(self._parents.get(item, ()))
        while pending:
            ancestor = pending.pop()
            if ancestor in seen:
                continue
            seen.add(ancestor)
            own = self._descendants(ancestor) | {ancestor}
            found |= {n for n in self._blocked_by.get(ancestor, ()) if n in self.open} - own
            pending.extend(self._parents.get(ancestor, ()))
        found.discard(item)
        self._memo[item] = tuple(sorted(found))
        return self._memo[item]

    def cycle_of(self, item: int) -> tuple[int, ...]:
        return self._in_cycle.get(item, ())

    def held(self, item: int) -> bool:
        return bool(self.blockers(item) or self.cycle_of(item))

    def reason(self, item: int) -> str:
        cycle = self.cycle_of(item)
        if cycle:
            return "in a dependency cycle: " + " -> ".join(f"#{n}" for n in (*cycle, cycle[0]))
        waiting = self.blockers(item)
        return "blocked by open " + ", ".join(f"#{n}" for n in waiting) if waiting else ""

    def cycles(self) -> tuple[tuple[int, ...], ...]:
        return self._cycles

    def _waits_on(self) -> dict[int, set[int]]:
        nodes = {n for found in self.links for n in (found.src, found.dst) if n in self.open}
        return {node: set(self.blockers(node)) for node in nodes if self.blockers(node)}

    def _find_cycles(self) -> tuple[tuple[int, ...], ...]:
        """Strongly connected groups of the open wait graph, iterative Tarjan."""
        edges = self._waits_on()
        index: dict[int, int] = {}
        low: dict[int, int] = {}
        stack: list[int] = []
        on_stack: set[int] = set()
        groups: list[tuple[int, ...]] = []
        counter = 0
        for root in sorted(edges):
            if root in index:
                continue
            work: list[tuple[int, list[int]]] = [(root, sorted(edges.get(root, ())))]
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                node, children = work[-1]
                if children:
                    child = children.pop(0)
                    if child not in index:
                        index[child] = low[child] = counter
                        counter += 1
                        stack.append(child)
                        on_stack.add(child)
                        work.append((child, sorted(edges.get(child, ()))))
                    elif child in on_stack:
                        low[node] = min(low[node], index[child])
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    group: list[int] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        group.append(member)
                        if member == node:
                            break
                    if len(group) > 1:
                        groups.append(_rotate(group))
        return tuple(sorted(groups))


def _rotate(group: list[int]) -> tuple[int, ...]:
    ordered = list(reversed(group))
    start = ordered.index(min(ordered))
    return tuple(ordered[start:] + ordered[:start])


def graph(issues: Iterable[Mapping[str, Any]], stored: Iterable[Link] = ()) -> Graph:
    """Stored links plus the links the open issue bodies declare right now."""
    rows = list(issues)
    open_numbers = {
        int(row["number"]) for row in rows if str(row.get("state") or "OPEN").upper() == "OPEN"
    }
    return Graph({*stored, *issue_body_links(rows)}, open_numbers)


def _store() -> Any:
    import factory_turso_core as core

    return core.store()


_WAITS = (
    "SELECT CASE kind WHEN 'blocks' THEN dst ELSE src END AS waiter,"
    " CASE kind WHEN 'blocks' THEN src ELSE dst END AS waited"
    " FROM factory_dep WHERE repo = :repo AND kind IN ('blocks', 'parent-child')"
)
_REACH = (
    f"WITH RECURSIVE waits(waiter, waited) AS ({_WAITS}),"
    " reach(n) AS (SELECT :waited UNION SELECT w.waited FROM waits w"
    " JOIN reach r ON w.waiter = r.n)"
)


def add(repo: str, src: int | str, dst: int | str, kind: str) -> str:
    """Store one factory link in one batch; the cycle check is part of the write."""
    found = link(src, dst, kind)
    params = {"repo": repo, "src": found.src, "dst": found.dst, "kind": found.kind}
    waiter, waited = (found.dst, found.src) if kind == "blocks" else (found.src, found.dst)
    inserted, present = _store().batch(
        [
            (
                f"{_REACH} INSERT INTO factory_dep (repo, src, dst, kind, origin, created_at)"
                " SELECT :repo, :src, :dst, :kind, 'factory', unixepoch()"
                " WHERE :kind NOT IN ('blocks', 'parent-child')"
                " OR NOT EXISTS (SELECT 1 FROM reach WHERE n = :waiter)"
                " ON CONFLICT (repo, src, dst, kind) DO UPDATE SET origin = 'factory'"
                " WHERE factory_dep.origin = 'body' RETURNING src",
                {**params, "waiter": waiter, "waited": waited},
            ),
            (
                "SELECT origin FROM factory_dep WHERE repo = :repo AND src = :src"
                " AND dst = :dst AND kind = :kind",
                params,
            ),
        ]
    )
    if inserted:
        return "added"
    if present:
        return "exists"
    raise DependencyCycle(f"#{waited} already waits on #{waiter}; {kind} would close a cycle")


def remove(repo: str, src: int | str, dst: int | str, kind: str) -> bool:
    found = link(src, dst, kind)
    (rows,) = _store().batch(
        [
            (
                "DELETE FROM factory_dep WHERE repo = :repo AND src = :src AND dst = :dst"
                " AND kind = :kind RETURNING src",
                {"repo": repo, "src": found.src, "dst": found.dst, "kind": found.kind},
            )
        ]
    )
    return bool(rows)


def read_links(repo: str) -> list[Link]:
    rows = _store().read(
        "SELECT src, dst, kind, origin FROM factory_dep WHERE repo = :repo"
        " ORDER BY src, dst, kind LIMIT :limit",
        {"repo": repo, "limit": MAX_LINKS},
    )
    return [Link(int(r["src"]), int(r["dst"]), str(r["kind"]), str(r["origin"])) for r in rows]


def stored(repo: str) -> list[Link]:
    """The stored links, or none when the store is off or unreadable; bodies still apply."""
    try:
        import factory_turso_core as core

        return read_links(repo) if core.enabled() else []
    except Exception as exc:  # noqa: BLE001 - body links still hold work without the store
        print(f"[factory-deps] stored links unavailable ({type(exc).__name__})", file=sys.stderr)
        return []


def blockers(repo: str, item: int | str, *, open_numbers: Collection[int]) -> list[int]:
    return list(Graph(read_links(repo), open_numbers).blockers(item_number(item)))


def reconcile(repo: str, issues: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Make the stored body links match the open issue bodies; writes only a change.

    One read of the stored links and at most one batch. A link whose waiting
    issue is no longer open is removed: a closed issue waits on nothing.
    """
    rows = list(issues)
    stored = read_links(repo)
    wanted = issue_body_links(rows)
    keys = {(found.src, found.dst, found.kind) for found in stored}
    stale = [found for found in stored if found.origin == "body" and found not in wanted]
    fresh = [found for found in sorted(wanted) if (found.src, found.dst, found.kind) not in keys]
    statements: list[tuple[str, dict[str, Any]]] = []
    for found in stale:
        statements.append(
            (
                "DELETE FROM factory_dep WHERE repo = :repo AND src = :src AND dst = :dst"
                " AND kind = :kind AND origin = 'body'",
                {"repo": repo, "src": found.src, "dst": found.dst, "kind": found.kind},
            )
        )
    for found in fresh:
        statements.append(
            (
                "INSERT INTO factory_dep (repo, src, dst, kind, origin, created_at)"
                " VALUES (:repo, :src, :dst, :kind, 'body', unixepoch())"
                " ON CONFLICT (repo, src, dst, kind) DO NOTHING",
                {"repo": repo, "src": found.src, "dst": found.dst, "kind": found.kind},
            )
        )
    statements = statements[:MAX_RECONCILE_WRITES]
    if statements:
        _store().batch(statements)
    current = [found for found in stored if found not in stale] + fresh
    cycles = graph(rows, current).cycles()
    for cycle in cycles:
        print(
            "[factory-deps] dependency cycle: " + " -> ".join(f"#{n}" for n in (*cycle, cycle[0])),
            file=sys.stderr,
        )
    return {
        "added": len(fresh),
        "removed": len(stale),
        "links": len(current),
        "cycles": [list(cycle) for cycle in cycles],
    }
