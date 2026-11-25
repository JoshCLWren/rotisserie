#!/usr/bin/env python3
"""Copy the factory's Neon state into the libSQL (Turso) store, once, with a count check.

    python factory_turso_migrate.py --repo OWNER/NAME --out summary.json [--dry-run]

Neon comes from ``FACTORY_STATE_DSN_FILE`` and is only read, inside one
read-only snapshot. The store comes from ``FACTORY_TURSO_URL`` with
``FACTORY_TURSO_TOKEN_FILE`` (or ``FACTORY_STATE_SQLITE``). A dry run reads
Neon, maps every row and writes nothing; it never opens the store.

Each batch of rows is one ``Store.batch`` transaction of
``INSERT ... ON CONFLICT DO NOTHING``, so an interrupted run is re-run from
the start: rows already copied are found, not duplicated. A stored row is
never overwritten. After each batch the tool counts the copied keys in the
store, so rows the factory writes natively do not read as a mismatch. A
completed run writes a marker event; a later real run refuses without
``--force``, because re-copying would bring back rows the factory has since
deleted (released claims, cleared backoffs, rolled-up attempts).

The summary holds counts, table names, row category names and provider
source names only. Errors print their class name only: messages could echo
a host or a value.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

import factory_libsql as libsql
from factory_turso_migrate_map import KEYS, PROJECTION, Mapped, insert_statement, map_kv
from factory_turso_migrate_neon import KEYSETS
from factory_turso_migrate_rows import map_event, map_table

KV = "factory_state_kv"
EVENTS = "factory_state_events"
STATE_TABLES = (
    "factory_model_health",
    "factory_worker_metrics",
    "factory_provider_budget",
    "factory_provider_reservation",
    "factory_pr_stage_retry",
    "factory_runtime_event_keys",
    "factory_model_attempt_daily",
)
COPIED = (KV, *STATE_TABLES, EVENTS)
RETIRED = ("factory_reconciliation_cursor", "factory_check_projection")
BUDGET_COLUMNS = ("concurrency_limit", "daily_limit", "cooldown_until", "quota_reset_at")
MARKER = "neon-migration:complete"
PAGE = 500
BATCH = 200
VERIFY_CHUNK = 100


class Source(Protocol):
    def tables(self) -> set[str]: ...
    def now(self) -> int: ...
    def count(self, table: str) -> int: ...
    def page(self, table: str, after: Sequence[Any] | None, limit: int) -> list[dict[str, Any]]: ...


def _group(summary: dict[str, Any], name: str, target: str) -> dict[str, Any]:
    return summary["migration"].setdefault(
        name,
        {"target": target, "source": 0, "eligible": 0, "inserted": 0, "present": 0, "skipped": {}},
    )


def _rows(source: Source, table: str, page: int) -> Iterator[dict[str, Any]]:
    after: tuple[Any, ...] | None = None
    keys = KEYSETS[table]
    while True:
        rows = source.page(table, after, page)
        yield from rows
        if len(rows) < page:
            return
        after = tuple(rows[-1][key] for key in keys)


def _membership(table: str, keys: list[tuple[Any, ...]]) -> libsql.Statement:
    columns = KEYS[table]
    params: dict[str, Any] = {}
    if len(columns) == 1:
        marks = []
        for index, key in enumerate(keys):
            params[f"k{index}"] = key[0]
            marks.append(f":k{index}")
        where = f"{columns[0]} IN ({', '.join(marks)})"
    else:
        tuples = []
        for index, key in enumerate(keys):
            names = [f"k{index}_{position}" for position in range(len(columns))]
            params.update(zip(names, key, strict=True))
            tuples.append(f"({', '.join(':' + name for name in names)})")
        where = f"({', '.join(columns)}) IN (VALUES {', '.join(tuples)})"
    return f"SELECT count(*) AS n FROM {table} WHERE {where}", params


class Copier:
    """Buffers mapped rows, writes them in bounded batches and counts them back."""

    def __init__(self, store: libsql.Store | None, summary: dict[str, Any], batch: int) -> None:
        self.store = store
        self.summary = summary
        self.batch = batch
        self.pending: list[tuple[str, Mapped]] = []
        self.seen: dict[str, set[tuple[Any, ...]]] = {}

    def add(self, mapped: Mapped) -> None:
        group = _group(self.summary, mapped.group, mapped.table or "")
        group["source"] += 1
        if mapped.values is None:
            group["skipped"][mapped.reason] = group["skipped"].get(mapped.reason, 0) + 1
            return
        key = mapped.key()
        seen = self.seen.setdefault(mapped.table, set())
        if key in seen:
            group["skipped"]["duplicate_key"] = group["skipped"].get("duplicate_key", 0) + 1
            return
        seen.add(key)
        if mapped.reason:
            notes = group.setdefault("notes", {})
            notes[mapped.reason] = notes.get(mapped.reason, 0) + 1
        group["target"] = mapped.table
        group["eligible"] += 1
        if self.store is not None:
            self.pending.append((mapped.group, mapped))
            if len(self.pending) >= self.batch:
                self.flush()

    def flush(self) -> None:
        if not self.pending or self.store is None:
            return
        pending, self.pending = self.pending, []
        statements = [insert_statement(m.table, m.values or {}) for _, m in pending]
        try:
            results = self.store.batch(statements)
        except libsql.StoreAmbiguous:
            results = self.store.batch(statements)
        groups = self.summary["migration"]
        for (name, _), rows in zip(pending, results, strict=True):
            groups[name]["inserted"] += len(rows)
        wanted: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        for name, mapped in pending:
            wanted.setdefault((name, mapped.table), []).append(mapped.key())
        checks: list[tuple[str, libsql.Statement]] = []
        for (name, table), keys in wanted.items():
            for start in range(0, len(keys), VERIFY_CHUNK):
                checks.append((name, _membership(table, keys[start : start + VERIFY_CHUNK])))
        found = self.store.reads([statement for _, statement in checks])
        for (name, _), rows in zip(checks, found, strict=True):
            groups[name]["present"] += int(rows[0]["n"])
        self.summary["batches"] += 1


def _budget_check(row: dict[str, Any]) -> dict[str, Any] | None:
    columns = [name for name in BUDGET_COLUMNS if row.get(name) is not None]
    return {"source": str(row.get("source") or ""), "columns": columns} if columns else None


def _copy_table(
    source: Source, copier: Copier, summary: dict[str, Any], table: str, repo: str, page: int
) -> int:
    now = source.now()
    read = 0
    for row in _rows(source, table, page):
        read += 1
        if table == KV:
            copier.add(map_kv(row))
        elif table == EVENTS:
            copier.add(map_event(row, repo))
        else:
            if table == "factory_provider_budget":
                found = _budget_check(row)
                if found:
                    summary["checks"]["provider_budget_set"].append(found)
            copier.add(map_table(table, row, now))
    copier.flush()
    return read


def _marker(repo: str) -> libsql.Statement:
    return (
        "INSERT INTO factory_state_events (repo, origin, event_type, event_id, created_at, detail)"
        " VALUES (:repo, 'rotisserie-migration', 'store_migrated', :event_id, unixepoch(),"
        " json_object('repo', :repo)) ON CONFLICT DO NOTHING RETURNING 1 AS x",
        {"repo": repo, "event_id": MARKER},
    )


def _migrated(store: libsql.Store) -> bool:
    return bool(
        store.read("SELECT 1 AS x FROM factory_state_events WHERE event_id = :e", {"e": MARKER})
    )


def migrate(
    source: Source,
    store: libsql.Store | None,
    *,
    repo: str,
    dry_run: bool,
    force: bool = False,
    page: int = PAGE,
    batch: int = BATCH,
) -> dict[str, Any]:
    """Copy (or, dry, only map and count) every Neon table; returns the summary."""
    started = time.monotonic()
    summary: dict[str, Any] = {
        "status": "ok",
        "dry_run": dry_run,
        "neon_tables": {},
        "absent_tables": [],
        "checks": {"projection_keys": 0, "provider_budget_set": []},
        "migration": {},
        "mismatches": [],
        "batches": 0,
    }
    if not dry_run:
        if store is None:
            raise ValueError("a real run needs the store")
        libsql.ensure_schema(store)
        if _migrated(store) and not force:
            summary["status"] = "already_migrated"
            return summary
    present = source.tables()
    for table in (*COPIED, *RETIRED):
        if table in present:
            summary["neon_tables"][table] = source.count(table)
        else:
            summary["absent_tables"].append(table)
    copier = Copier(None if dry_run else store, summary, batch)
    for table in COPIED:
        if table not in present:
            continue
        read = _copy_table(source, copier, summary, table, repo, page)
        if read != summary["neon_tables"][table]:
            summary["mismatches"].append({"table": table, "check": "rows_read"})
    for table in RETIRED:
        if table in present:
            count = summary["neon_tables"][table]
            group = _group(summary, table, "")
            group["source"] = count
            if count:
                group["skipped"]["retired_table"] = count
    not_migrated = summary["migration"].get("kv:not_migrated", {}).get("skipped", {})
    summary["checks"]["projection_keys"] = int(not_migrated.get(PROJECTION, 0))
    summary["checks"]["provider_budget_set"].sort(key=lambda item: item["source"])
    for name, group in sorted(summary["migration"].items()):
        if group["source"] != group["eligible"] + sum(group["skipped"].values()):
            summary["mismatches"].append({"group": name, "check": "source_split"})
        if not dry_run and group["present"] != group["eligible"]:
            summary["mismatches"].append({"group": name, "check": "destination_count"})
    if summary["mismatches"]:
        summary["status"] = "mismatch"
    elif not dry_run and store is not None:
        store.batch([_marker(repo)])
    summary["seconds"] = round(time.monotonic() - started, 2)
    return summary


def _write(path: Path | None, summary: dict[str, Any]) -> None:
    if path is not None:
        path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="repository for events that name none")
    parser.add_argument("--dry-run", action="store_true", help="read and map only; write nothing")
    parser.add_argument("--force", action="store_true", help="run again after a completed run")
    parser.add_argument("--out", type=Path, help="write the JSON summary here")
    parser.add_argument("--page", type=int, default=PAGE)
    parser.add_argument("--batch", type=int, default=BATCH)
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.batch <= 1000 or not 1 <= arguments.page <= 5000:
        parser.error("--batch is 1 to 1000 and --page is 1 to 5000")
    summary: dict[str, Any] = {"status": "failed", "dry_run": arguments.dry_run}
    try:
        from factory_turso_migrate_neon import NeonSource, dsn

        store = None if arguments.dry_run else libsql.connect()
        with NeonSource(dsn()) as source:
            summary = migrate(
                source,
                store,
                repo=arguments.repo,
                dry_run=arguments.dry_run,
                force=arguments.force,
                page=arguments.page,
                batch=arguments.batch,
            )
    except Exception as error:
        summary = {**summary, "status": "failed", "error": type(error).__name__}
    _write(arguments.out, summary)
    print(f"Store migration: {summary['status']}")
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    sys.exit(main())
