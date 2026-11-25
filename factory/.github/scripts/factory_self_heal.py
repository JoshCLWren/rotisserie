#!/usr/bin/env python3
"""Clear the stalls the factory can clear by itself, and say so on the PR.

Run by the trusted dispatcher once per tick (``sweep``):

* moves a PR back to review when its repair verdict named no finding, so the
  next review runs on another model (``avoided-reviewers`` tells selection
  which one);
* merges main into a ``factory:merge-conflict`` PR that GitHub now reports as
  mergeable, once per head and only while nobody holds the PR;
* resolves a bot reviewer's thread that a factory worker answered "Fixed" with
  a commit that is in the current head, and the factory's own line-anchored
  finding thread once a later commit changed the lines it cites;
* parks a PR that loops (``factory_item_loop``): no more workers go to it at
  its current head, in any stage, until its owner acts;
* sends a PR parked for 12 hours one forced run, then ends the park or closes
  the PR and requeues its issue (``factory_self_heal_park_retry``).

Every action leaves a trusted comment (see ``factory_self_heal_rules``), which
``baste heal`` and ``baste recap`` read, and an event in the factory store.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import factory_crash
import factory_self_heal_rules as rules

OWNER_LABEL_RE = re.compile(r"^factory:(?:[0-9]+|local)$")
CLAIMED = "factory:claimed"
EVENT_TYPE = "factory_self_heal"


class GitHub(Protocol):
    repo: str

    def api(self, path: str, *args: str, payload: object | None = None) -> Any: ...

    def comments(self, number: int) -> list[dict[str, Any]]: ...

    def graphql(self, query: str, **variables: object) -> dict[str, Any]: ...


class Store(Protocol):
    def enabled(self) -> bool: ...

    def record(self, entries: list[dict[str, Any]]) -> int: ...

    def holder(self, number: int) -> str | None: ...

    def loop_evidence(self, seconds: int) -> tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]:
        ...


class GhCli:
    """GitHub over the ``gh`` CLI, as every trusted factory script reaches it."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _run(self, args: list[str], payload: object | None = None) -> str:
        proc = subprocess.run(
            ["gh", *args],
            input=None if payload is None else json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if proc.returncode:
            message = (proc.stderr or proc.stdout).strip() or f"gh {args[0]} failed"
            raise factory_crash.GitHubCallError(message)
        return proc.stdout

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        extra = ["--input", "-"] if payload is not None else []
        out = self._run(["api", f"repos/{self.repo}/{path}", *args, *extra], payload)
        return json.loads(out or "null")

    def comments(self, number: int) -> list[dict[str, Any]]:
        out = self._run(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{self.repo}/issues/{number}/comments?per_page=100",
            ]
        )
        pages = json.loads(out or "[]")
        return [
            row for page in pages if isinstance(page, list) for row in page if isinstance(row, dict)
        ]

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        data = json.loads(self._run(args) or "{}")
        if not isinstance(data, dict) or data.get("errors"):
            raise RuntimeError(
                f"GraphQL failed: {data.get('errors') if isinstance(data, dict) else data}"
            )
        return data


class LedgerStore:
    """The factory store through its public ledger functions (trusted jobs only)."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def enabled(self) -> bool:
        import factory_state_ledger as ledger

        return bool(ledger.enabled())

    def record(self, entries: list[dict[str, Any]]) -> int:
        import factory_state_ledger as ledger

        return int(ledger.record_events(self.repo, entries))

    def holder(self, number: int) -> str | None:
        import factory_state_ledger as ledger

        if not ledger.enabled():
            return None
        row = ledger.target_lease(self.repo, number)
        if row:
            return f"worker {row['worker']}" if row.get("worker") else "a factory operation"
        outside = ledger.external_holder(self.repo, number)
        if outside:
            return f"outside agent {outside.get('client') or 'unknown'}"
        return None

    def loop_evidence(self, seconds: int) -> tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]:
        from factory_turso_loops import evidence

        return evidence(self.repo, seconds)


def labels(row: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for label in row.get("labels") or []:
        name = label.get("name") if isinstance(label, Mapping) else label
        if isinstance(name, str):
            found.add(name)
    return found


def label_holder(names: set[str]) -> str | None:
    """Who holds the PR by its labels: a worker owner label or a person's claim."""
    owners = sorted(name for name in names if OWNER_LABEL_RE.fullmatch(name))
    if owners:
        return f"owner label {owners[0]}"
    agents = sorted(name for name in names if name.startswith("agent:"))
    if agents:
        return f"outside agent label {agents[0]}"
    return "a person's claim (factory:claimed)" if CLAIMED in names else None


def event(
    *, kind: str, pr: int, head: str, ref: str, text: str, **detail: object
) -> dict[str, Any]:
    # Both store backends file events under an integer worker subject; 0 is the factory itself.
    return {
        "event_type": EVENT_TYPE,
        "event_id": f"self-heal:{kind}:{pr}:{ref}",
        "worker": "0",
        "kind": kind,
        "pr": pr,
        "head": head,
        "ref": ref,
        "text": text,
        "observed_at": datetime.now(UTC).isoformat(),
        **detail,
    }


def report(
    github: GitHub,
    store: Store,
    *,
    kind: str,
    pr: int,
    head: str,
    ref: str,
    text: str,
    avoid: str = "",
    detail: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Make one self-heal visible: a trusted PR comment, then a store event."""
    body = rules.comment_body(kind=kind, pr=pr, head=head, ref=ref, text=text, avoid=avoid)
    github.api(f"issues/{pr}/comments", "--method", "POST", payload={"body": body})
    row: dict[str, Any] = {"kind": kind, "pr": pr, "head": head, "ref": ref, "text": text}
    try:
        store.record(
            [event(kind=kind, pr=pr, head=head, ref=ref, text=text, avoid=avoid, **(detail or {}))]
        )
    except Exception as exc:  # noqa: BLE001 - the PR comment already makes it visible
        row["event_error"] = type(exc).__name__
    return row


def open_pulls(github: GitHub) -> list[dict[str, Any]]:
    rows = github.api("pulls?state=open&per_page=100", "--paginate", "--slurp")
    pages = rows if isinstance(rows, list) else []
    flat = [row for page in pages for row in (page if isinstance(page, list) else [page])]
    return [row for row in flat if isinstance(row, dict) and "factory" in labels(row)]


def sweep(
    github: GitHub,
    store: Store,
    *,
    max_syncs: int = 3,
    max_actions: int = 10,
    dry_run: bool = False,
    sync: Any = None,
) -> dict[str, Any]:
    import factory_self_heal_actions as actions

    pulls = open_pulls(github)
    budget = actions.Budget(actions=max_actions, syncs=max_syncs, dry_run=dry_run)
    from factory_self_heal_loops import park_loops, sync_parked
    from factory_self_heal_park_retry import resolve_long_parks
    from factory_self_heal_incident import clear_closed, sync_incident_work
    from factory_self_heal_state import sync_state

    out: dict[str, Any] = {
        "dry_run": dry_run, "verdicts": [], "threads": [], "conflicts": [], "loops": [],
        "park_retries": [],
        "parked_labels": [], "state_labels": [], "incident_labels": [], "closed_labels": [],
    }
    for name, step in (
        ("loops", park_loops),
        ("park_retries", resolve_long_parks),
        ("verdicts", actions.heal_empty_verdicts),
        ("threads", actions.resolve_answered_threads),
        ("parked_labels", sync_parked),
        ("state_labels", sync_state),
        ("incident_labels", sync_incident_work),
        ("closed_labels", clear_closed),
    ):
        try:
            out[name] = step(github, store, pulls, budget)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            out[name] = [{"error": f"{type(exc).__name__}: {exc}"}]
    try:
        out["conflicts"] = actions.sync_conflicts(github, store, pulls, budget, sync=sync)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        out["conflicts"] = [{"error": f"{type(exc).__name__}: {exc}"}]
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("sweep", help="clear every self-healable stall once")
    run.add_argument("--max-syncs", type=int, default=3, help="most merge-main writes per pass")
    run.add_argument("--max-actions", type=int, default=10, help="most other writes per pass")
    run.add_argument("--dry-run", action="store_true", help="report, change nothing")
    avoided = commands.add_parser(
        "avoided-reviewers", help="models the next review of a PR must not use (JSON)"
    )
    avoided.add_argument("--pr", type=int, required=True)
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    github = GhCli(args.repo)
    if args.command == "avoided-reviewers":
        print(json.dumps(rules.avoided_reviewers(github.comments(args.pr), pr=args.pr)))
        return 0
    result = sweep(
        github,
        LedgerStore(args.repo),
        max_syncs=max(0, args.max_syncs),
        max_actions=max(0, args.max_actions),
        dry_run=args.dry_run,
    )
    for name in ("loops", "park_retries", "verdicts", "threads", "conflicts"):
        for row in result[name]:
            print(
                f"self-heal {name}: {row.get('text') or row.get('error') or row}", file=sys.stderr
            )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    import factory_crash
    raise SystemExit(factory_crash.run(main))
