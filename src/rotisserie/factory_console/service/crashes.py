"""Factory crashes grouped by fingerprint, for ``baste crashes``, turning, doctor and both UIs.

Every factory entry point stores one ``factory_crash`` row per uncaught
exception (``factory_crash.py``). The status page job publishes the last day's
groups, without tracebacks, as ``crashes`` inside ``factory-stats.json``; this
machine reads that. The newest full (scrubbed) traceback of one group is
readable only inside a trusted job: directly when the store is configured
here, else through the read-only operator workflow.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from rotisserie.factory_console.github import GitHub, Obj, items, obj
from rotisserie.factory_console.service import bridge, reliability
from rotisserie.factory_console.service.sync import ledger_directories, store_configured
from rotisserie.factory_console.tracker import DISPATCH_WORKFLOW, Clock

READ_WORKFLOW = "rotisserie-operator-read.yml"
READ_ARTIFACT = "rotisserie-operator-{run_id}"
READ_TIMEOUT = 600.0
PUBLISHED_HOURS = 24
KEY_WORKFLOWS = {
    DISPATCH_WORKFLOW: "dispatcher",
    "free-model-factory-dispatch.yml": "dispatcher",
    "factory-ready-merge-drain.yml": "merge drain",
}
CHECK = "factory crashes"
HINT = "baste crashes"


class CrashesUnavailableError(RuntimeError):
    """The factory store is not reachable from here."""


def moment(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def headline(group: Mapping[str, Any]) -> str:
    where = f"{group.get('script')}:{group.get('line')} {group.get('function')}"
    return f"{group.get('exception_type')} in {where}"


def where_ran(group: Mapping[str, Any]) -> str:
    names = group.get("workflows") or [group.get("workflow")]
    known = [KEY_WORKFLOWS.get(str(name), str(name)) for name in names if name]
    return ", ".join(dict.fromkeys(known))


def within(groups: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    """Each group recounted over the occurrences at or after ``since``; empty groups drop."""
    found: list[dict[str, Any]] = []
    for group in groups:
        times = [at for at in (moment(t) for t in group.get("times") or []) if at is not None]
        recent = sorted(at for at in times if at >= since)
        if not recent:
            continue
        count = len(recent)
        if count == len(times) and int(group.get("count") or 0) > count:
            count = int(group["count"])
        found.append({**group, "count": count, "first_at": recent[0].isoformat()})
    return found


def from_stats(stats: Obj | None) -> dict[str, Any]:
    block = obj((stats or {}).get("crashes"))
    if not block:
        return {
            "source": "published",
            "published": False,
            "groups": [],
            "reasons": ["no crash groups have been published yet"],
        }
    groups = [dict(item) for item in items(block, "groups")]
    return {
        "source": "published",
        "published": True,
        "generated_at": block.get("generated_at"),
        "window_hours": block.get("window_hours") or PUBLISHED_HOURS,
        "truncated": bool(block.get("truncated")),
        "groups": groups,
    }


def published(github: GitHub) -> dict[str, Any]:
    return from_stats(reliability.load_stats(github))


def _modules() -> tuple[Any, Any]:
    if not store_configured():
        raise CrashesUnavailableError(
            "The factory store is visible only to the trusted factory runtime"
        )
    for directory in ledger_directories():
        sys.path.insert(0, str(directory))
        try:
            import factory_crash_store as store  # type: ignore[import-not-found]
            import factory_libsql as libsql  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CrashesUnavailableError(f"the factory runtime could not load ({exc})") from exc
        finally:
            sys.path.remove(str(directory))
        return store, libsql
    raise CrashesUnavailableError("the factory runtime was not found (run from an adopter)")


def live(repo: str, hours: int) -> dict[str, Any]:
    store, libsql = _modules()
    found = dict(store.export(libsql.connect(), repo, hours=hours))
    return {**found, "source": "store", "published": True}


def show_direct(repo: str, fingerprint: str) -> dict[str, Any]:
    store, libsql = _modules()
    found = store.newest(libsql.connect(), repo, fingerprint)
    if found is None:
        return {"fingerprint": fingerprint, "found": False, "source": "store"}
    return {**dict(found), "found": True, "source": "store"}


def show_via_github(
    github: GitHub, clock: Clock, fingerprint: str, *, started: dict[str, object] | None = None
) -> dict[str, Any]:
    request = json.dumps(
        {
            "operation": "read",
            "reads": [{"name": "crash-trace", "query": {"fingerprint": fingerprint}}],
        }
    )
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
    if not payload or result.data.get("error") or "found" not in payload:
        reason = result.data.get("error") or payload.get("error") or "the read returned no crash"
        raise bridge.BridgeError(str(reason), url=result.url)
    found = {key: value for key, value in payload.items() if key != "exit_code"}
    return {**found, "source": "operator read workflow", "bridge": result.summary()}


def burning(
    groups: list[dict[str, Any]], now: datetime, window: timedelta
) -> dict[str, Any] | None:
    """The newest fatal crash of the dispatcher or merge drain inside ``window``."""
    for group in within(groups, now - window):
        names = set(group.get("workflows") or [group.get("workflow")])
        if group.get("fatal") and names & set(KEY_WORKFLOWS):
            return group
    return None


def burning_text(group: Mapping[str, Any], window: timedelta) -> str:
    minutes = int(window.total_seconds() // 60)
    times = "once" if group.get("count") == 1 else f"{group.get('count')} times"
    return (
        f"{headline(group)} crashed the {where_ran(group)} {times} in the last {minutes}m: "
        f"{group.get('message') or 'no message'}"
    )


def health(stats: Obj | None, now: datetime) -> tuple[str, str, str]:
    """(status, text, hint) for doctor: a dispatcher or drain crash fails, any other warns."""
    report = from_stats(stats)
    if not report["published"]:
        return "ok", "no crash groups published yet", ""
    recent = within(report["groups"], now - timedelta(hours=1))
    fatal = [group for group in recent if group.get("fatal")]
    hot = burning(report["groups"], now, timedelta(hours=1))
    if hot is not None:
        return "fail", burning_text(hot, timedelta(hours=1)), HINT
    if fatal:
        names = "; ".join(f"{headline(g)} ({g.get('count')}x)" for g in fatal[:3])
        return "warn", f"{len(fatal)} crash groups in the last hour: {names}", HINT
    day = [group for group in report["groups"] if group.get("fatal")]
    return "ok", f"no crash in the last hour; {len(day)} crash groups in the last day", ""
