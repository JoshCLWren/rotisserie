"""``baste ready``: the dispatcher's ranked pool of claimable work, as plain data.

The ranking is ``factory_ready.pool``, the function the dispatcher itself walks
(through ``frontier.capture``). Retry cooling, loop parks and factory-written
dependency links live in the factory store; when this process cannot read the
store the answer says so and is not authoritative.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import Obj
from rotisserie.factory_console.service import sync as branch_sync

NO_STORE = (
    "The factory store is not readable here, so retry cooling, loop parks and "
    "factory-written dependency links were not checked; the dispatcher may hold more. "
    "Ask through GitHub (--via github) for the store's view."
)


@dataclass
class StoreInputs:
    links: list[Any] = field(default_factory=list)
    holds: Callable[[list[Any]], Mapping[int, str]] | None = None
    prs: list[Obj] = field(default_factory=list)
    checked: bool = False
    note: str = NO_STORE


@contextmanager
def store_inputs(repo: str, issues: list[Obj], prs: list[Obj]) -> Iterator[StoreInputs]:
    """The store-side pool inputs, with the adopter's store scripts importable meanwhile."""
    directories = branch_sync.ledger_directories() if branch_sync.store_configured() else []
    if not directories:
        yield StoreInputs(prs=prs)
        return
    path = str(directories[0])
    sys.path.insert(0, path)
    try:
        try:
            import factory_dispatch_holds as holds  # type: ignore[import-not-found]

            links = runtime.module("factory_deps").read_links(repo)
            stamped = holds.stamp(repo, prs, issues)
        except Exception as exc:  # noqa: BLE001 - the answer says the store was not read
            yield StoreInputs(prs=prs, note=f"The factory store read failed ({exc}). {NO_STORE}")
            return
        yield StoreInputs(links, holds.reader(repo, stamped), stamped, True, "")
    finally:
        sys.path.remove(path)


def dependencies(repo: str, issues: list[Obj], number: int | None = None) -> dict[str, object]:
    """Blockers, cycles and the links touching one item, from bodies and the store."""
    deps = runtime.module("factory_deps")
    with store_inputs(repo, issues, []) as store:
        graph = deps.graph(issues, store.links)
    data: dict[str, object] = {
        "store_checked": store.checked,
        "notes": [store.note] if store.note else [],
        "cycles": [list(cycle) for cycle in graph.cycles()],
    }
    if number is not None:
        data["blocked_by"] = list(graph.blockers(number))
        data["cycle"] = list(graph.cycle_of(number))
        data["reason"] = graph.reason(number)
        data["links"] = [
            found.to_dict() for found in graph.links if number in (found.src, found.dst)
        ]
    return data


def _items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def project(snapshot: Mapping[str, object], limit: int = 20) -> dict[str, object]:
    """The ready items of one work frontier, in the dispatcher's order."""
    offers = _items(snapshot.get("offers"))
    ready = [offer for offer in offers if offer.get("ready") is True]
    held = [
        {"target": offer.get("target"), "reasons": list(_items_text(offer.get("hold_reasons")))}
        for offer in offers
        if offer.get("ready") is not True
    ]
    held += [
        {"target": hold.get("target"), "reasons": list(_items_text(hold.get("reasons")))}
        for hold in _items(snapshot.get("holds"))
    ]
    notes = [str(note) for note in _items_text(snapshot.get("notes"))]
    store_checked = snapshot.get("store_checked") is True
    return {
        "repository": snapshot.get("repository"),
        "generated_at": snapshot.get("generated_at"),
        "authoritative": store_checked,
        "notes": notes,
        "ready": [
            {
                "rank": position,
                "target": offer.get("target"),
                "title": offer.get("title"),
                "stage": offer.get("stage"),
                "action": offer.get("action"),
                "lane": offer.get("lane"),
                "priority": offer.get("priority"),
                "url": offer.get("url"),
                "lease": offer.get("lease"),
            }
            for position, offer in enumerate(ready[: max(limit, 0)], 1)
        ],
        "ready_count": len(ready),
        "held": held,
        "cycles": [list(cycle) for cycle in _cycles(snapshot.get("cycles"))],
    }


def _items_text(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [str(item) for item in value]


def _cycles(value: object) -> list[list[int]]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    found: list[list[int]] = []
    for cycle in value:
        if isinstance(cycle, Sequence) and not isinstance(cycle, str):
            found.append([int(n) for n in cycle if isinstance(n, int)])
    return found
