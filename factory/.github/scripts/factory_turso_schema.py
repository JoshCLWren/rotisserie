#!/usr/bin/env python3
"""Schema status and upgrade for the factory store, standard library only.

``status`` only reads, so it works with the read-only store token: each table
and index the schema file creates and whether it exists, the stored
``_schema_version`` against this code's version, and the last upgrade attempt
recorded in ``_schema_upgrade``. ``apply`` runs every schema statement now
(each is ``IF NOT EXISTS`` or ``INSERT OR IGNORE``), records the result and
reads the status back. Both print one JSON object with no URL or token.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

import factory_libsql as libsql


def _iso(epoch: object) -> str | None:
    if not isinstance(epoch, int):
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _last(store: libsql.Store) -> dict[str, Any] | None:
    rows = store.read(
        "SELECT version, result, error, created, source, workflow, run_id, at"
        " FROM _schema_upgrade WHERE name = 'last'"
    )
    if not rows:
        return None
    row = dict(rows[0])
    created = row.get("created")
    row["created"] = json.loads(created) if isinstance(created, str) else None
    row["at"] = _iso(row.get("at"))
    return row


def _reasons(
    missing: list[str], stored: int | None, last: dict[str, Any] | None
) -> tuple[str, list[str]]:
    code = libsql.SCHEMA_VERSION
    reasons: list[str] = []
    states: list[str] = []
    if missing:
        more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
        noun = "object is" if len(missing) == 1 else "objects are"
        reasons.append(f"{len(missing)} expected {noun} missing: {', '.join(missing[:5])}{more}")
        states.append("missing")
    if stored is None or stored < code:
        reasons.append(f"stored schema version {stored or 'none'} is behind this code's {code}")
        states.append("behind")
    elif stored > code:
        reasons.append(f"stored schema version {stored} is newer than this code's {code}")
        states.append("ahead")
    if last and last.get("result") == "failed":
        reasons.append(f"the last upgrade failed: {last.get('error') or 'no error recorded'}")
        states.append("failed")
    return (states[0] if states else "ok"), reasons


def status(store: libsql.Store) -> dict[str, Any]:
    """What the store holds against what this code expects. Reads only."""
    present = libsql.present_objects(store)
    objects = [
        {"kind": kind, "name": name, "present": name in present}
        for kind, name in libsql.expected_objects()
    ]
    missing = [str(item["name"]) for item in objects if not item["present"]]
    stored: int | None = None
    if "_schema_version" in present:
        rows = store.read("SELECT max(version) AS v FROM _schema_version")
        value = rows[0]["v"] if rows else None
        stored = int(value) if value is not None else None
    last = _last(store) if "_schema_upgrade" in present else None
    state, reasons = _reasons(missing, stored, last)
    return {
        "status": state,
        "reasons": reasons,
        "code_version": libsql.SCHEMA_VERSION,
        "stored_version": stored,
        "objects": objects,
        "missing": missing,
        "last_upgrade": last,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def apply(store: libsql.Store) -> dict[str, Any]:
    """Run the idempotent schema now, then report what the store holds."""
    error = ""
    try:
        libsql.ensure_schema(store, force=True, source="apply")
    except libsql.StoreBlocked:
        raise
    except libsql.StoreError as exc:
        error = libsql.scrub(str(exc))
    report = status(store)
    return {**report, "applied": "failed" if error else "ok", "apply_error": error or None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "apply"))
    arguments = parser.parse_args(argv)
    if not libsql.configured():
        print(json.dumps({"status": "not_configured", "reasons": []}))
        return 0
    try:
        store = libsql.connect()
        report = apply(store) if arguments.command == "apply" else status(store)
    except libsql.StoreError as exc:
        print(json.dumps({"status": "unavailable", "reasons": [libsql.scrub(str(exc))]}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 1 if report.get("applied") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
