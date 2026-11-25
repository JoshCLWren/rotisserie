"""Closed-issue memories: written on close, related by links, parents and files, capped in the prompt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import factory_deps as deps
import factory_issue_intake_guard as guard
import factory_memory as memory
import factory_post_merge_closure as closure
import pytest

from tests import turso_support as support


@pytest.fixture(params=support.kinds())
def store(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def pr(number: int, body: str = "Adds the thing.") -> dict[str, Any]:
    return {"number": number, "title": f"Fix part {number}", "body": body}


def test_a_summary_is_prose_only_and_capped() -> None:
    body = (
        "<!-- hidden marker -->Moves the cache.\n```python\nprint('x')\n```\n"
        "https://claude.ai/code/session_01ABC\n" + "word " * 2000
    )
    found = memory.summarize(
        7, "Warm the cover cache", pr(70, body), "LGTM\x07 ship it", ["a/b.py"]
    )
    assert len(found.summary) <= memory.SUMMARY_CAP
    assert "hidden marker" not in found.summary and "print(" not in found.summary
    assert "claude.ai" not in found.summary and "\x07" not in found.summary
    assert found.summary.startswith("PR #70: Fix part 70. Moves the cache.")
    assert found.files == ("a/b.py",) and found.prs == (70,)


def test_closing_an_issue_stores_its_memory_once(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db, repo = store
    reads: list[list[str]] = []

    def graphql(args: list[str]) -> dict[str, Any]:
        reads.append(args)
        return {
            "data": {
                "repository": {
                    "issue": {"title": "Warm the cover cache"},
                    "pullRequest": {
                        "reviews": {"nodes": [{"body": "Looks right."}, {"body": ""}]},
                        "files": {"nodes": [{"path": "src/cache.py"}, {"path": "tests/t.py"}]},
                    },
                }
            }
        }

    monkeypatch.setattr(memory, "_gh_json", graphql)
    info = {"title": "Warm the cache", "body": "Keeps covers warm.", "branch": "factory/6-7-x"}
    assert memory.remember_closure(repo, 7, 70, info) is True
    assert memory.remember_closure(repo, 7, 70, info) is False
    assert len(reads) == 1
    (found,) = memory.read(repo)
    assert found.title == "Warm the cover cache"
    assert found.summary == "PR #70: Warm the cache. Keeps covers warm. Final review: Looks right."
    assert found.files == ("src/cache.py", "tests/t.py")


def test_decide_remembers_only_a_closed_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    info = {"branch": "factory/6-7-warm", "body": "Closes #7", "title": "Warm", "state": "MERGED"}
    monkeypatch.setattr(closure.github, "_pr_info", lambda _n: info)
    monkeypatch.setattr(closure.github, "_repo_slug", lambda: "o/r")
    monkeypatch.setattr(
        closure.memory,
        "remember_closure",
        lambda _r, issue, number, _i: calls.append((issue, number)),
    )
    for outcome in ("closed", "already-closed", "open-dependency"):
        monkeypatch.setattr(closure, "close_merged_pr_issue", lambda *_a, o=outcome, **_k: o)
        closure.decide(70)
    assert calls == [(7, 70)]


def test_old_memories_compact_to_one_line(store: Any) -> None:
    db, repo = store
    memory.record(repo, memory.summarize(7, "Warm the cache", pr(70), "", ["src/a.py"]))
    memory.record(repo, memory.summarize(8, "Fresh work", pr(80), "", []))
    db.batch(
        [
            (
                "UPDATE factory_memory SET closed_at = unixepoch() - 31 * 86400"
                " WHERE repo = :repo AND issue = 7",
                {"repo": repo},
            )
        ]
    )
    assert memory.compact(repo) == 1
    assert memory.compact(repo) == 0
    rows = {found.issue: found for found in memory.read(repo)}
    assert rows[7].summary == "Warm the cache (PR #70)" and rows[7].files == ("src/a.py",)
    assert rows[8].summary.startswith("PR #80")


def memories() -> list[memory.Memory]:
    return [
        memory.Memory(1, "Blocker", "PR #11: did the base.", (), (11,), 100),
        memory.Memory(2, "Sibling", "PR #12: did a sibling.", (), (12,), 200),
        memory.Memory(3, "Same file", "PR #13: touched the cache.", ("src/cache.py",), (13,), 300),
        memory.Memory(4, "Unrelated", "PR #14: elsewhere.", ("docs/x.md",), (14,), 400),
        memory.Memory(5, "Bare name", "PR #15: also cache.", ("lib/cache.py", "a/b.ts"), (15,), 50),
    ]


LINKS = [
    deps.Link(1, 9, "blocks"),
    deps.Link(20, 9, "parent-child"),
    deps.Link(20, 2, "parent-child"),
]


def test_related_memories_come_from_links_parents_and_files() -> None:
    body = "Change `src/cache.py` and cache.py callers."
    rows = memory.related(9, body, memories(), LINKS)
    assert [(row.memory.issue, row.why) for row in rows] == [
        (1, "blocks this issue"),
        (2, "same parent #20"),
        (3, "same files: src/cache.py"),
        (5, "same files: lib/cache.py"),
    ]


def test_the_prompt_context_is_capped_and_labelled_as_background(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    many = [
        memory.Memory(n, f"Issue {n}", "x" * 600, ("src/cache.py",), (n,), n) for n in range(1, 60)
    ]
    text = memory.render(memory.related(9, "src/cache.py", many, []))
    assert text.startswith(memory.PREFIX) and len(text) <= memory.CONTEXT_CAP
    assert text.count("#") < 59
    monkeypatch.setattr(guard, "_merged", lambda _issue, _repo: "")
    monkeypatch.setenv(memory.CONTEXT_ENV, json.dumps(text))
    assert guard.main(["merged-context", "9", "--repo", "o/r"]) == 0
    printed = capsys.readouterr().out
    assert printed == " " + text and len(printed) <= memory.CONTEXT_CAP + 1
    monkeypatch.delenv(memory.CONTEXT_ENV)
    assert guard.main(["merged-context", "9", "--repo", "o/r"]) == 0
    assert capsys.readouterr().out == ""


def test_context_reads_the_issue_body_links_and_memories(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db, repo = store
    memory.record(repo, memory.summarize(1, "Blocker", pr(11), "", []))
    deps.add(repo, 1, 9, "blocks")
    monkeypatch.setattr(memory, "_gh_json", lambda _args: {"body": "No files named."})
    text = memory.context(repo, 9)
    assert "#1 Blocker [blocks this issue]: PR #11" in text
