"""Tests for read-only live host-view acquisition."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "factory_rotisserie_capture.py"


def load_capture() -> ModuleType:
    """Load the workflow-side capture helper."""
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("factory_rotisserie_capture", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_capture_contract() -> None:
    capture = load_capture()
    commands: list[list[str]] = []

    def run_json(command: list[str]) -> object:
        commands.append(command)
        if command[1:3] == ["issue", "list"]:
            return [
                {
                    "number": 10,
                    "title": "Work",
                    "body": "",
                    "state": "OPEN",
                    "labels": [{"name": "factory"}, {"name": "factory:7"}],
                    "createdAt": "2026-09-28T00:00:00Z",
                    "updatedAt": "2026-09-28T00:10:00Z",
                },
                {"number": 11, "labels": [{"name": "bug"}]},
            ]
        if command[1:3] == ["pr", "list"]:
            return [
                {
                    "number": 20,
                    "title": "Fix #10",
                    "body": "Closes #10",
                    "state": "OPEN",
                    "isDraft": False,
                    "labels": [{"name": "factory"}],
                    "createdAt": "2026-09-28T00:00:00Z",
                    "updatedAt": "2026-09-28T00:10:00Z",
                    "headRefName": "factory/8-10-work",
                    "headRefOid": "a" * 40,
                }
            ]
        if command[1:3] == ["pr", "view"]:
            return {"statusCheckRollup": [{"name": "test", "conclusion": "SUCCESS"}]}
        return [
            {
                "user": {"login": "github-actions[bot]"},
                "author_association": "NONE",
                "body": "<!-- comic-pile-factory-semantic-review-v1:pr-20:head-"
                + ("a" * 40)
                + ":reviewer-9:producer-8:verdict-approve -->",
            },
            {
                "user": {"login": "github-actions[bot]"},
                "author_association": "NONE",
                "body": "<!-- comic-pile-factory-head-contributor-v1:pr-20:head-"
                + ("a" * 40)
                + ":worker-8:epoch-17 -->",
            },
            {
                "user": {"login": "github-actions[bot]"},
                "author_association": "NONE",
                "body": "<!-- comic-pile-factory-head-contributor-v1:pr-20:head-"
                + ("9" * 40)
                + ":worker-6:epoch-19 -->",
            },
        ]

    view = capture.capture_view(revision="b" * 40, captured_at=100, run_json=run_json)

    assert view["revision"] == "b" * 40
    assert len(view["issues"]) == 1
    assert view["issues"][0]["lease"]["expires_at"] > view["issues"][0]["lease"]["acquired_at"]
    assert view["pull_requests"][0]["checks"][0]["status"] == "passed"
    assert view["pull_requests"][0]["reviews"][0]["head"] == "a" * 40
    # The captured lineage spans every recorded head of the PR, not just the
    # current one, so the shadow adapter blocks the same reviewers the real
    # controller blocks.
    assert view["pull_requests"][0]["head_contributors"] == ["6", "8"]
    assert all("create" not in command and "edit" not in command for command in commands)


def test_untrusted_authors_cannot_supply_contributor_provenance() -> None:
    """Only controller-trusted comment authors feed captured provenance."""
    capture = load_capture()
    marker = (
        "<!-- comic-pile-factory-head-contributor-v1:pr-20:head-"
        + ("a" * 40)
        + ":worker-8:epoch-17 -->"
    )

    def run_json(command: list[str]) -> object:
        if command[1:3] == ["issue", "list"]:
            return []
        if command[1:3] == ["pr", "list"]:
            return [
                {
                    "number": 20,
                    "labels": [{"name": "factory"}],
                    "headRefName": "factory/8-20-work",
                    "headRefOid": "a" * 40,
                }
            ]
        if command[1:3] == ["pr", "view"]:
            return {"statusCheckRollup": []}
        return [{"user": {"login": "somebody-else"}, "author_association": "CONTRIBUTOR", "body": marker}]

    view = capture.capture_view(revision="b" * 40, captured_at=100, run_json=run_json)

    assert view["pull_requests"][0]["head_contributors"] == []
