"""The factory store's schema: what exists, the stored version and the last upgrade.

The status read works directly inside a trusted job that holds the store
connection, or through the read-only operator workflow (``--via github``),
which runs the same read with the read-only store token. ``--apply`` runs the
idempotent schema through the label claim workflow, which holds the write
token. The status page job publishes the status as ``schema`` inside
``factory-stats.json``; doctor reads that published value.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from rotisserie.factory_console.github import GitHub, Obj, obj, timestamp
from rotisserie.factory_console.service import bridge
from rotisserie.factory_console.service.sync import ledger_directories, store_configured
from rotisserie.factory_console.tracker import Clock

READ_WORKFLOW = "rotisserie-operator-read.yml"
READ_ARTIFACT = "rotisserie-operator-{run_id}"
READ_TIMEOUT = 600.0
APPLY_WORKFLOW = "rotisserie-label-claim.yml"
APPLY_ARTIFACT = "rotisserie-label-claim-{run_id}"
APPLY_MODE = "schema-apply"
STALE = timedelta(hours=1)
HINT = "baste schema --via github reads it; baste schema --apply --via github repairs it"


class SchemaUnavailableError(RuntimeError):
    """The factory store is not reachable from here."""


def _module(env: Mapping[str, str] | None = None) -> Any:
    if not store_configured(env):
        raise SchemaUnavailableError(
            "The factory store is visible only to the trusted factory runtime; use --via github"
        )
    for directory in ledger_directories():
        sys.path.insert(0, str(directory))
        try:
            import factory_turso_schema as schema  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SchemaUnavailableError(f"the factory runtime could not load ({exc})") from exc
        finally:
            sys.path.remove(str(directory))
        return schema
    raise SchemaUnavailableError("the factory runtime was not found (run from an adopter)")


def read_direct() -> dict[str, object]:
    module = _module()
    return dict(module.status(module.libsql.connect()))


def apply_direct() -> dict[str, object]:
    module = _module()
    return dict(module.apply(module.libsql.connect()))


def read_via_github(
    github: GitHub, clock: Clock, *, started: dict[str, object] | None = None
) -> dict[str, object]:
    request = json.dumps({"operation": "read", "reads": [{"name": "schema"}]})
    result = bridge.run(
        github,
        clock,
        READ_WORKFLOW,
        {"request": request},
        artifact=READ_ARTIFACT,
        timeout=READ_TIMEOUT,
        started=started,
    )
    reads = result.data.get("reads")
    answer = reads[0] if isinstance(reads, list) and reads else {}
    payload = obj(answer.get("result")) if isinstance(answer, dict) else {}
    if not payload or result.data.get("error"):
        reason = result.data.get("error") or "the read workflow returned no schema status"
        raise bridge.BridgeError(str(reason), url=result.url)
    found = {key: value for key, value in payload.items() if key != "exit_code"}
    if payload.get("exit_code") not in (0, None) and "objects" not in found:
        reason = found.get("error") or "the schema read failed in the trusted workflow"
        raise bridge.BridgeError(str(reason), url=result.url)
    return {**found, "source": "operator read workflow", "bridge": result.summary()}


def apply_via_github(
    github: GitHub, clock: Clock, *, started: dict[str, object] | None = None
) -> dict[str, object]:
    result = bridge.run(
        github,
        clock,
        APPLY_WORKFLOW,
        {"mode": APPLY_MODE},
        artifact=APPLY_ARTIFACT,
        timeout=bridge.CLAIM_TIMEOUT,
        started=started,
    )
    return {**result.data, "source": "label claim workflow", "bridge": result.summary()}


def from_stats(stats: Obj | None) -> dict[str, Any]:
    report = obj((stats or {}).get("schema"))
    if not report:
        return {"status": "unpublished", "reasons": ["no schema status has been published yet"]}
    return {**report, "source": "published"}


def health(stats: Obj | None, now: datetime) -> tuple[str, str, str]:
    """(status, text, hint) for the doctor line, from the published value only."""
    report = from_stats(stats)
    state = str(report.get("status"))
    if state == "unpublished":
        return "ok", "no store schema status published yet", ""
    if state == "not_configured":
        return "ok", "no factory store is configured", ""
    reasons = "; ".join(str(r) for r in report.get("reasons") or []) or state
    checked = timestamp(report, "checked_at")
    if state != "ok":
        return "warn", reasons, HINT
    if checked is None or now - checked > STALE:
        return "warn", "published store schema status is stale", HINT
    stored = report.get("stored_version")
    return "ok", f"every expected table and index exists; version {stored}", ""
