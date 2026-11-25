"""Portable backup: copy the factory store to a plain SQLite file, or restore one.

The file uses the shipped schema, so any SQLite tool, a local factory or a new
libSQL database can read it. Rows are read in key order, in pages, so a large
table never needs one huge response. Restore refuses a non-empty target.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import factory_libsql as libsql

PAGE = 500
TABLES = {
    "factory_lease": ("lease_key",),
    "factory_provider_reservation": ("claim_id",),
    "factory_provider_budget": ("repo", "source"),
    "factory_backoff": ("backoff_key",),
    "factory_repo_state": ("repo",),
    "factory_model_health": ("repo", "source", "model"),
    "factory_pr_stage_retry": ("repo", "pr_number", "head_sha", "stage"),
    "factory_worker_metrics": ("repo", "worker", "source", "model"),
    "factory_runtime_event_keys": ("repo", "run_id"),
    "factory_state_events": ("id",),
    "factory_model_attempt_daily": ("repo", "day", "source", "model", "outcome_class"),
    "factory_cursor": ("repo", "name"),
    "factory_crash": ("id",),
}


def _pages(db: libsql.Store, table: str, key: tuple[str, ...], repo: str | None) -> Any:
    order = ", ".join(key)
    after: tuple[Any, ...] | None = None
    while True:
        clauses, params = [], {"limit": PAGE}
        if repo is not None:
            clauses.append("repo = :repo")
            params["repo"] = repo
        if after is not None:
            marks = ", ".join(f":a{i}" for i in range(len(key)))
            clauses.append(f"({order}) > ({marks})")
            params.update({f"a{i}": value for i, value in enumerate(after)})
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = db.read(f"SELECT * FROM {table}{where} ORDER BY {order} LIMIT :limit", params)
        if not rows:
            return
        yield rows
        if len(rows) < PAGE:
            return
        after = tuple(rows[-1][column] for column in key)


def export(db: libsql.Store, destination: Path, *, repo: str | None = None) -> dict[str, int]:
    """Write every table (or one repository's rows) to a new SQLite file."""
    if destination.exists():
        raise FileExistsError(f"{destination} already exists")
    out = sqlite3.connect(destination)
    counts: dict[str, int] = {}
    try:
        out.executescript(libsql.SCHEMA.read_text(encoding="utf-8"))
        for table, key in TABLES.items():
            counts[table] = 0
            for rows in _pages(db, table, key, repo):
                columns = list(rows[0])
                out.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)})"
                    f" VALUES ({', '.join('?' for _ in columns)})",
                    [tuple(row[c] for c in columns) for row in rows],
                )
                counts[table] += len(rows)
        out.commit()
        if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("exported file failed its integrity check")
    finally:
        out.close()
    return counts


def restore(source: Path, db: libsql.Store) -> dict[str, int]:
    """Load an exported file into an empty store, in bounded batches."""
    libsql.ensure_schema(db)
    for table in TABLES:
        if db.read(f"SELECT 1 AS x FROM {table} LIMIT 1"):
            raise RuntimeError(f"restore target is not empty ({table})")
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        for table, key in TABLES.items():
            cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {', '.join(key)}")
            counts[table] = 0
            while rows := cursor.fetchmany(100):
                columns = rows[0].keys()
                sql = (
                    f"INSERT INTO {table} ({', '.join(columns)})"
                    f" VALUES ({', '.join(':' + c for c in columns)})"
                )
                db.batch([(sql, dict(row)) for row in rows])
                counts[table] += len(rows)
    finally:
        conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "restore"))
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--repo")
    args = parser.parse_args(argv)
    db = libsql.connect()
    if args.command == "export":
        counts = export(db, args.file, repo=args.repo)
    else:
        counts = restore(args.file, db)
    summary = {"mode": args.command, "schema": 1, "status": "ok", "rows": counts, "usage": db.usage}
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    sys.exit(main())
