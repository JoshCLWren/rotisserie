"""Shared GitHub read cache: one read serves many panels, and writes never see it."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from rotisserie import cli
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.read_cache import ReadCache, classify, pure_read

ROOT = Path(__file__).resolve().parents[1]
REPO = "acme/widgets"


@pytest.mark.parametrize(
    ("args", "kind"),
    [
        (["api", "repos/acme/widgets/issues"], "cache"),
        (["api", "--allow-escape-sequences", "repos/acme/widgets/actions/jobs/1/logs"], "cache"),
        (["api", "-X", "GET", "search/issues", "-f", "q=x"], "cache"),
        (["api", "graphql", "-f", "query=query { viewer { login } }"], "cache"),
        (["api", "graphql", "-f", "query=mutation { addLabel }"], "write"),
        (["api", "repos/acme/widgets/issues/1/labels", "-f", "labels[]=x"], "write"),
        (["api", "--method", "DELETE", "repos/acme/widgets/issues/1/labels/x"], "write"),
        (["issue", "list", "--repo", REPO], "cache"),
        (["variable", "list", "--repo", REPO], "cache"),
        (["secret", "list", "--repo", REPO], "read"),
        (["variable", "set", "X", "--body", "1"], "write"),
        (["workflow", "run", "x.yml"], "write"),
        (["issue", "edit", "1", "--add-label", "x"], "write"),
    ],
)
def test_only_reads_are_storable_and_everything_else_is_a_write(args: list[str], kind: str) -> None:
    assert classify(args) == kind


@pytest.mark.parametrize(
    ("argv", "pure"),
    [
        (["status"], True),
        (["recap", "--since", "12h"], True),
        (["doctor"], True),
        (["doctor", "--fix"], False),
        (["providers", "disable", "x", "--reason", "y"], False),
        (["models", "disable", "x", "y"], False),
        (["models", "x"], True),
        (["settings", "FACTORY_LIMIT", "3"], False),
        (["leases", "--release-expired"], False),
        (["unstick", "--dry-run"], False),
        (["incident", "start", "issue:1"], False),
        (["follow", "pr:1"], False),
        (["follow", "pr:1", "--once"], True),
        (["run", "x"], False),
        (["claim", "pr:1"], False),
        (["reserve-next", "--client", "a"], False),
        (["backoffs"], False),
    ],
)
def test_the_cache_is_offered_only_to_commands_that_cannot_write(
    argv: list[str], pure: bool
) -> None:
    assert pure_read(cli.parser().parse_args(["factory", *argv])) is pure


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def cache(tmp_path: Path, max_age: float = 60) -> tuple[ReadCache, Clock]:
    clock = Clock()
    return ReadCache(
        tmp_path / "rotisserie" / "gh-reads" / "acme__widgets", max_age=max_age, clock=clock
    ), clock


def test_a_read_is_served_within_its_age_and_refetched_after_it(tmp_path: Path) -> None:
    store, clock = cache(tmp_path)
    fetched: list[str] = []

    def fetch() -> str:
        fetched.append("x")
        return f"body {len(fetched)}"

    assert store.read_through("k", fetch) == "body 1"
    clock.now += 59
    assert store.read_through("k", fetch) == "body 1"
    assert store.oldest == 1_000_000.0
    clock.now += 2
    assert store.read_through("k", fetch) == "body 2"
    assert len(fetched) == 2


def test_a_write_orphans_mutable_entries_but_not_final_ones(tmp_path: Path) -> None:
    store, _ = cache(tmp_path)
    store.read_through("list", lambda: "old list")
    store.read_through("log", lambda: "finished log", final=True)
    store.invalidate()
    assert store.read_through("list", lambda: "new list") == "new list"
    assert store.read_through("log", lambda: "refetched", final=True) == "finished log"


def test_refresh_bypasses_the_cache_and_still_shares_its_answer(tmp_path: Path) -> None:
    shared, clock = cache(tmp_path)
    shared.read_through("k", lambda: "old")
    refresh = ReadCache(shared.directory, max_age=0, clock=clock)
    assert refresh.read_through("k", lambda: "new") == "new"
    assert shared.read_through("k", lambda: "unused") == "new"


def test_a_fetch_that_started_before_a_write_is_never_served_after_it(tmp_path: Path) -> None:
    store, _ = cache(tmp_path)

    def racing_fetch() -> str:
        store.invalidate()
        return "read before the write landed"

    store.read_through("k", racing_fetch)
    assert store.read_through("k", lambda: "after the write") == "after the write"


def test_concurrent_panels_make_one_request(tmp_path: Path) -> None:
    store, _ = cache(tmp_path)
    calls: list[int] = []

    def slow() -> str:
        calls.append(1)
        time.sleep(0.3)
        return "issues"

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(store.read_through("k", slow)))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == ["issues"] * 4
    assert len(calls) == 1


def test_failed_reads_are_not_stored(tmp_path: Path) -> None:
    store, _ = cache(tmp_path)

    def broken() -> str:
        raise RuntimeError("GitHub is down")

    with pytest.raises(RuntimeError):
        store.read_through("k", broken)
    assert store.read_through("k", lambda: "ok") == "ok"


def test_the_cache_is_private_bounded_and_holds_no_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_secret_token_value")
    folder = tmp_path / "rotisserie" / "gh-reads" / "acme__widgets"
    folder.parent.mkdir(parents=True, mode=0o755)
    store = ReadCache(folder, max_age=60, max_total=3000)
    github = GitHub(REPO)
    github.use_cache(store, reads=True)
    monkeypatch.setattr(
        GitHub,
        "_run",
        lambda self, args, stdin=None, allow_exit_one=False: json.dumps([os.urandom(900).hex()]),
    )
    for number in range(6):
        github.api(f"repos/{REPO}/issues/{number}")
    for path in (folder, folder.parent, folder.parent.parent):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    entries = list(folder.glob("*.gz"))
    assert entries and sum(entry.stat().st_size for entry in entries) <= 3000
    for entry in folder.iterdir():
        assert stat.S_IMODE(entry.stat().st_mode) & 0o077 == 0
        assert b"ghp_secret" not in entry.read_bytes()
        assert b"Authorization" not in entry.read_bytes()


def test_a_write_in_the_client_invalidates_and_ends_cached_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Sequence[str]] = []

    def run(self: GitHub, args: Sequence[str], stdin: str | None = None, **_: object) -> str:
        calls.append(list(args))
        return json.dumps({"n": len(calls)})

    monkeypatch.setattr(GitHub, "_run", run)
    store, _ = cache(tmp_path)
    reader = GitHub(REPO)
    reader.use_cache(store, reads=True)
    assert reader.repo_api("issues") == reader.repo_api("issues")
    assert len(calls) == 1
    writer = GitHub(REPO)
    writer.use_cache(ReadCache(store.directory), reads=False)
    writer.gh("issue", "edit", "1", "--add-label", "x")
    assert reader.repo_api("issues") == {"n": 3}
    reader.repo_api("issues/1/labels", method="POST", fields={"labels[]": "y"})
    assert not reader.cached_reads
    reader.repo_api("issues")
    reader.repo_api("issues")
    assert len(calls) == 6


def test_write_paths_never_read_cached_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Sequence[str]] = []

    def run(self: GitHub, args: Sequence[str], stdin: str | None = None, **_: object) -> str:
        calls.append(list(args))
        return "{}"

    monkeypatch.setattr(GitHub, "_run", run)
    store, _ = cache(tmp_path)
    warm = GitHub(REPO)
    warm.use_cache(store, reads=True)
    warm.repo_api("issues")
    lease = GitHub(REPO)
    lease.use_cache(ReadCache(store.directory, max_age=3600), reads=False)
    lease.repo_api("issues")
    lease.text("actions/jobs/1/logs", final="completed")
    assert len(calls) == 3


def test_the_browser_store_serves_refreshes_drops_and_bounds_panel_reads() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [
            node,
            str(ROOT / "tests" / "web_store_harness.cjs"),
            str(ROOT / "src" / "rotisserie" / "web_ui.js"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    out = json.loads(result.stdout)
    assert out["withinTtl"]["requests"] == ["/api/status"]
    assert out["withinTtl"]["asOfIsServerReadTime"]
    assert out["stale"]["requests"] == ["/api/status", "/api/status"]
    assert out["stale"]["rerendered"] == ["Status"]
    assert out["fresh"] == "/api/status?fresh=1"
    assert out["targetedReadsNotStored"] == 2
    assert out["afterDrop"] == ["doctor?"]
    assert out["failuresAndJobsNotStored"] == ["doctor?", "doctor?failing=1"]
    assert out["bounded"]["total"] <= 2_000_000 and out["bounded"]["newestKept"]
    assert out["blockedStorage"] == {"exit_code": 0, "requests": 1}
    assert out["readCrossingAWriteNotStored"] == 0
    assert out["unknownViewer"] == {"stored": 0, "requests": 2}
    assert out["hostedKey"] == "rotisserie:v1:acme/widgets:alice:status?"
    assert out["hostedPublicIsSeparate"] == 4
    assert out["afterSignOut"] == ["rotisserie:v1:acme/widgets:public:status?"]
