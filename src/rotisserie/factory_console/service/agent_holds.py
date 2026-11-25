"""Outside agent holds: an ``agent:NAME`` label keeps the factory off an item.

An outside agent adds the label itself, so nobody else ends a hold that the agent
abandons. The hourly label doctor removes an ``agent:*`` label once the item has
had no activity (commits, comments or label changes) for a day, with one comment
saying why, so the item returns to the factory. It reuses the doctor's listing
of open items and makes no other read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from rotisserie.factory_console.github import GitHub, GitHubError, GitHubNotFoundError, string

PREFIX = "agent:"
STALE_SECONDS = 24 * 3600


def labels_of(row: dict[str, Any]) -> list[str]:
    return sorted(
        str(label.get("name"))
        for label in row.get("labels") or []
        if isinstance(label, dict) and str(label.get("name", "")).startswith(PREFIX)
    )


def stale(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Open items whose agent hold has seen no activity for a day."""
    found = []
    for row in rows:
        agents = labels_of(row)
        updated = string(row, "updated_at")
        if not agents or not updated:
            continue
        idle = (now - datetime.fromisoformat(updated.replace("Z", "+00:00"))).total_seconds()
        if idle >= STALE_SECONDS:
            found.append({"number": int(row["number"]), "agents": agents, "idle": int(idle)})
    return found


def release(github: GitHub, rows: list[dict[str, Any]], now: datetime) -> list[dict[str, object]]:
    """Remove each stale ``agent:*`` label and say why on the item."""
    done: list[dict[str, object]] = []
    for item in stale(rows, now):
        number = int(item["number"])
        agents = list(item["agents"])
        try:
            for name in agents:
                try:
                    github.gh(
                        "api",
                        f"repos/{github.repo}/issues/{number}/labels/{quote(name, safe='')}",
                        "--method",
                        "DELETE",
                    )
                except GitHubNotFoundError:
                    pass
            names = ", ".join(f"`{name}`" for name in agents)
            github.gh(
                "api",
                f"repos/{github.repo}/issues/{number}/comments",
                "--method",
                "POST",
                "-f",
                f"body=Removed {names}: no commits, comments or other activity here for "
                "24 hours, so the factory takes this item back. Add the label again to "
                "resume work on it.",
            )
        except GitHubError as exc:
            done.append(
                {"number": number, "agents": agents, "status": "failed", "reason": str(exc)}
            )
            continue
        done.append({"number": number, "agents": agents, "status": "released"})
    return done
