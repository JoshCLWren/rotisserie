"""The recap names merged factory PRs whose issue a worker can still take."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from rotisserie.factory_console.service import merged_open
from rotisserie.factory_console.service import recap_build as build

NOW = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)


def merged_pr(number: int, title: str, body: str, *labels: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": name} for name in labels],
        "pull_request": {"merged_at": "2026-11-25T02:47:23Z"},
    }


class Hub:
    repo = "acme/widgets"

    def __init__(self, branches: dict[int, str], issues: dict[int, dict[str, Any]]) -> None:
        self.branches = branches
        self.issues = issues
        self.reads: list[str] = []

    def repo_api(self, route: str) -> Any:
        self.reads.append(route)
        kind, number = route.split("/")
        if kind == "pulls":
            return {"head": {"ref": self.branches[int(number)]}}
        found = self.issues[int(number)]
        labels = [{"name": name} for name in found.get("labels", ["factory", "factory:unowned"])]
        return {
            **found,
            "labels": labels,
            "body": "Fix it.",
            "html_url": f"https://github.com/acme/widgets/issues/{number}",
        }


def comicpile_merges() -> tuple[Hub, list[dict[str, Any]]]:
    hub = Hub(
        {
            5756: "factory/5751-read-now-roll-parity",
            5766: "freebuff/aad5cd343387904c5c4f8a4c",
            5770: "factory/12-5760-kilo-auto",
            5771: "factory/13-5761-kilo-auto",
            5663: "factory/work-checkpoints/issue-4171/hy4-flash-free/1",
            5529: "local-factory/sv1misc",
        },
        {
            5751: {
                "state": "open",
                "title": "read now on roll screen",
                "labels": ["factory", "factory:1236998703301", "factory:building"],
            },
            4171: {
                "state": "open",
                "title": "CBL browser",
                "labels": ["factory", "factory:unowned", "factory:blocked"],
            },
            4335: {"state": "open", "title": "Rust cutover", "labels": ["factory", "epic"]},
            5695: {"state": "open", "title": "Creator credits remain queued"},
            5760: {"state": "open", "title": "Stranded fix"},
            5761: {"state": "closed", "title": "Closed fix"},
        },
    )
    merged = [
        merged_pr(5756, "Read now opens roll details (#5751)", "Part of #5751.", "factory"),
        merged_pr(5766, "Normalize COMICVINE_API_KEY (#5695)", "Part of #5695.", "factory"),
        merged_pr(5770, "Fix the stranded thing", "", "factory"),
        merged_pr(5771, "Fix the closed thing", "", "factory"),
        merged_pr(5772, "A person's change (#5751)", "Part of #5751."),
        merged_pr(5663, "CBL browser fix", "Part of #4171.", "factory"),
        merged_pr(5529, "Part of #4335: move callers", "Part of #4335.", "factory"),
    ]
    return hub, merged


def test_scan_keeps_merged_factory_prs_whose_issue_is_open() -> None:
    hub, merged = comicpile_merges()
    rows = merged_open.scan(hub, merged)  # type: ignore[arg-type]
    assert [
        (row["pr"], row["issue"], row["kind"], row["closes"], row["owned"]) for row in rows
    ] == [
        (5756, 5751, "non-closing", False, True),
        (5766, 5695, "non-closing", False, False),
        (5770, 5760, "factory-branch", True, False),
    ]
    assert "pulls/5772" not in hub.reads


def test_the_recap_asks_the_owner_about_each_one() -> None:
    hub, merged = comicpile_merges()
    sources = build.Sources(since=NOW - timedelta(hours=12), now=NOW)
    sources.open_after_merge = merged_open.scan(hub, merged)  # type: ignore[arg-type]
    rows = [row for row in build.needs_you(sources) if row["kind"] == "merged-open"]
    assert len(rows) == 3
    assert str(rows[0]["text"]).endswith("A worker already took it again.")
    assert rows[1]["text"] == (
        "Normalize COMICVINE_API_KEY (#5766) merged. It says it is only part of it, so "
        "Creator credits remain queued (#5695) stays open. Workers can take it again."
    )
    assert rows[1]["action"] == "If the merge finished it, close it: `gh issue close 5695`."
    assert rows[1]["url"] == "https://github.com/acme/widgets/issues/5695"
    assert "The next merge drain closes it" in str(rows[2]["action"])
