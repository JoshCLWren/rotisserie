"""Facts about an exact commit, kept across jobs in one store row.

A commit's changed files and the existence of a workflow run for it never
change once seen, yet the merge drain read them again on every run: 23
``pulls/N/files`` listings and 43 per-head workflow run counts per drain (runs
45640887858, 45641592506, 45642321381). Only facts that cannot flip back are
stored. The row is pruned to the heads still open. A store error leaves the
memo empty, which costs only the old live reads.
"""

from __future__ import annotations

import sys
import time
from typing import Any


class HeadMemo:
    def __init__(self, repo: str, name: str) -> None:
        self.repo = repo
        self.name = name
        self.facts: dict[str, Any] = {}
        self.cursor: int | None = None
        self.changed = False
        self.usable = False
        try:
            import factory_turso_core as core

            if not core.enabled():
                return
            import factory_turso_cursors as cursors

            found = cursors.get(repo, name)
        except Exception as exc:
            print(f"[factory-head-memo] {name} unavailable ({type(exc).__name__})", file=sys.stderr)
            return
        self.usable = True
        if found is not None:
            self.cursor = found[0]
            facts = found[1].get("facts")
            self.facts = dict(facts) if isinstance(facts, dict) else {}

    def get(self, sha: str) -> Any:
        return self.facts.get(sha)

    def put(self, sha: str, value: Any) -> None:
        if self.facts.get(sha) != value:
            self.facts[sha] = value
            self.changed = True

    def save(self, keep: set[str]) -> None:
        """Store the facts for ``keep`` only; nothing is written when nothing changed."""
        kept = {sha: value for sha, value in self.facts.items() if sha in keep}
        if not self.usable or (not self.changed and kept == self.facts):
            return
        now = int(time.time())
        at = max(now, self.cursor + 1) if self.cursor is not None else now
        try:
            import factory_turso_cursors as cursors

            cursors.advance(self.repo, self.name, at, {"facts": kept})
        except Exception as exc:
            print(
                f"[factory-head-memo] {self.name} not saved ({type(exc).__name__})", file=sys.stderr
            )
