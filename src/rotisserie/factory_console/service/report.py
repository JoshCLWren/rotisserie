"""Plain-word pieces of the factory report that other reads (report, recap) share."""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import quote

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError, Obj, items, obj, string
from rotisserie.factory_console.service import reliability

OUTCOME_WORDS = {
    "provider_quota": "provider out of quota",
    "provider_failure": "provider error",
    "model_retired_410": "model retired by provider",
    "control_plane_failure": "factory setup problem",
    "no_capacity": "no capacity",
    "work_failure": "model could not finish the work",
    "no_progress": "model made no progress",
    "timeout": "ran out of time",
    "agent_timeout": "ran out of time",
    "unknown_failure": "unclassified failure",
    "unconfigured": "provider not configured",
}
NO_CAPACITY = "no capacity: "


def search(github: GitHub, query: str) -> list[Obj]:
    return items(github.api(f"search/issues?q={quote(query)}&per_page=100&sort=updated"), "items")


def split_capacity(
    found: Counter[tuple[str, str]],
) -> tuple[Counter[tuple[str, str]], Counter[tuple[str, str]]]:
    """Capacity exits apart from real failures: (no capacity, failures)."""
    capacity = Counter({key: n for key, n in found.items() if key[0].startswith(NO_CAPACITY)})
    return capacity, found - capacity


def plain_failure(message: str) -> str:
    """Turn the factory's outcome sentence into a short, plain reason."""
    if message.startswith("No capacity: "):
        return NO_CAPACITY + message.removeprefix("No capacity: ")[:120]
    match = re.search(r"worker failed: ([a-z_0-9]+)", message)
    if match is None:
        return message[:120]
    reason = OUTCOME_WORDS.get(match.group(1), match.group(1).replace("_", " "))
    if message.startswith("Code saved"):
        reason += "; its code was saved on the pull request"
    return reason


def main_failing_checks(github: GitHub) -> list[str]:
    try:
        repo = obj(github.repo_api(""))
        branch = string(repo, "default_branch") or "main"
        checks = items(github.repo_api(f"commits/{branch}/check-runs?per_page=100"), "check_runs")
    except GitHubError:
        return []
    return sorted(
        {
            string(check, "name")
            for check in checks
            if string(check, "conclusion") in ("failure", "timed_out", "startup_failure")
        }
    )


def provider_rows(stats: Obj, window: str) -> list[list[str]]:
    """[provider, runs, delivered, done, idle, failed] per provider, best first."""
    rows = []
    for entry in items(stats, "providers"):
        counts = reliability.window(entry, window)
        provider = runtime.provider_for_source(string(entry, "source"))
        rows.append(
            [
                provider,
                str(reliability.count(counts, "runs")),
                str(reliability.count(counts, "delivered")),
                str(
                    reliability.count(counts, "completed") - reliability.count(counts, "delivered")
                ),
                str(reliability.count(counts, "idle")),
                str(reliability.count(counts, "failed")),
            ]
        )
    return sorted(rows, key=lambda row: (-int(row[2]), -int(row[1])))
