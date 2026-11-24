"""One overview page load: panels share GitHub reads instead of each repeating them."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from test_factory_console import (  # type: ignore[import-not-found]
    FakeGitHub,
    fake,  # noqa: F401
    invoke,
)

from rotisserie.factory_console.read_cache import ReadCache
from rotisserie.web_jobs import Jobs

OVERVIEW = (
    ["providers"],
    ["status"],
    ["report", "--since", "12h"],
    ["doctor"],
    ["why"],
    ["recap", "--since", "12h"],
    ["heal", "--since", "24h"],
)
FAILED_RUNS = {
    "workflow_runs": [
        {
            "id": 500 + n,
            "name": "Free model factory",
            "display_title": f"Factory {n} worker",
            "status": "completed",
            "conclusion": "failure",
            "event": "workflow_dispatch",
            "run_attempt": 1,
            "created_at": "2026-11-21T22:30:00Z",
            "updated_at": "2026-11-21T22:40:00Z",
        }
        for n in range(4)
    ]
}
JOBS = {
    "jobs": [
        {
            "id": 70,
            "name": "run / factory",
            "status": "completed",
            "conclusion": "failure",
            "steps": [{"name": "Run worker", "conclusion": "failure"}],
        },
        {
            "id": 71,
            "name": "Persist trusted worker outcome",
            "status": "completed",
            "conclusion": "success",
            "steps": [],
        },
    ]
}


def page_load(gh: FakeGitHub, *flags: str) -> list[list[str]]:
    start = len(gh.calls)
    for argv in OVERVIEW:
        _, out = invoke(["--json", *flags, *argv])
        assert json.loads(out)["command"] == argv[0], out
    calls: list[list[str]] = gh.calls[start:]
    return calls


def factory(gh: FakeGitHub) -> FakeGitHub:
    gh.routes[r"actions/workflows/[^/]+/runs"] = FAILED_RUNS
    gh.routes[r"actions/runs/\d+/jobs"] = JOBS
    gh.routes["issue-list"] = [{"number": 3, "title": "Fix it", "labels": [], "body": ""}]
    gh.texts["logs"] = "Traceback: boom\n"
    return gh


def test_one_overview_load_reads_each_github_resource_once(
    fake: FakeGitHub,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = page_load(factory(fake))
    unique = {json.dumps(call) for call in before}
    monkeypatch.setenv("ROTISSERIE_CACHE_DIR", str(tmp_path / "empty"))
    cold = page_load(fake, "--max-age", "60")
    warm = page_load(fake, "--max-age", "60")
    # Recorded on this fake factory: 95 calls (30 distinct) before, 34 cold, 1 on a reload.
    assert len(before) >= 90
    assert len(cold) <= 40 and len(cold) <= len(unique) + 5
    assert len(warm) <= 3
    assert all(call[:2] == ["secret", "list"] or "workflow" in call for call in warm), warm


def test_refresh_rereads_what_changes_but_not_finished_job_logs(fake: FakeGitHub) -> None:  # noqa: F811
    page_load(factory(fake), "--max-age", "60")
    refresh = page_load(fake, "--max-age", "0")
    assert not any("logs" in " ".join(call) for call in refresh)
    assert any(call[:2] == ["issue", "list"] for call in refresh)


def test_a_write_command_never_uses_the_cache(fake: FakeGitHub) -> None:  # noqa: F811
    page_load(factory(fake), "--max-age", "60")
    start = len(fake.calls)
    invoke(["--max-age", "60", "doctor", "--fix"])
    fix = fake.calls[start:]
    start = len(fake.calls)
    invoke(["doctor"])
    plain = fake.calls[start:]
    assert len(fix) >= len(plain) > 0


def test_a_change_from_the_web_ui_invalidates_shared_reads(tmp_path: Path) -> None:
    store = ReadCache(tmp_path / "reads", max_age=60)
    store.read_through("issues", lambda: "before the claim")
    jobs = Jobs(
        "acme/widgets",
        command=lambda repo, args: [sys.executable, "-c", "print('{}')"],
        cache=store,
    )
    read = jobs.start(["status"], mutating=False, max_age=60)
    while read.state == "running":
        time.sleep(0.02)
    assert store.read_through("issues", lambda: "unused") == "before the claim"
    write = jobs.start(["claim", "pr:1", "--no-wait"], mutating=True, max_age=60)
    assert write.max_age == 0
    while write.state == "running":
        time.sleep(0.02)
    assert store.read_through("issues", lambda: "after the claim") == "after the claim"
