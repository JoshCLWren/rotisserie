"""``rotisserie factory disk``: measure, judge and reclaim, all inside tmp dirs."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from rotisserie import cli
from rotisserie.factory_console import commands
from rotisserie.factory_console.service import disk, doctor
from rotisserie.factory_console.service.disk import (
    DiskConfigError,
    DiskReport,
    DiskSettings,
    Item,
    Judge,
    ProcProcesses,
    Scanner,
    Volume,
)

OLD = time.time() - 30 * 24 * 3600
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Rotisserie Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Rotisserie Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def run_git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "init.defaultBranch=main", *args],
        check=True,
        capture_output=True,
        text=True,
        env=disk.git_environment(os.environ),
    ).stdout


def age(path: Path, when: float = OLD) -> None:
    """Make ``path`` and everything under it look untouched since ``when``."""
    for directory, names, files in os.walk(path):
        for name in [*names, *files]:
            os.utime(Path(directory) / name, (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


@dataclass
class FakeProcesses:
    pids: set[int] = field(default_factory=set)
    used: list[Path] = field(default_factory=list)

    def alive(self, pid: int) -> bool:
        return pid in self.pids

    def paths_in_use(self) -> tuple[Path, ...]:
        return tuple(path.resolve() for path in self.used)


@dataclass
class Host:
    """A miniature factory host: a repository, its worktrees, targets and caches."""

    base: Path
    repo: Path
    worktrees: Path
    targets: Path
    cache: Path
    ledger: Path

    def worktree(self, name: str, *, push: bool = True) -> Path:
        path = self.worktrees / name
        run_git(self.repo, "worktree", "add", "-q", "-b", name, str(path))
        (path / f"{name}.txt").write_text(name)
        run_git(path, "add", ".")
        run_git(path, "commit", "-q", "-m", name)
        if push:
            run_git(path, "push", "-q", "origin", name)
        return path

    def settings(self, **changes: object) -> DiskSettings:
        values: dict[str, object] = {
            "worktree_roots": (str(self.worktrees),),
            "target_roots": (str(self.targets),),
            "build_dirs": (str(self.base / "code" / "*" / "target"),),
            "caches": (str(self.cache),),
            "volumes": (str(self.base),),
            "ledger": str(self.ledger),
        }
        values.update(changes)
        return DiskSettings(**values)  # type: ignore[arg-type]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "code" / "app"
    run_git(tmp_path, "init", "-q", "--bare", str(origin))
    run_git(tmp_path, "clone", "-q", str(origin), str(repo))
    (repo / ".gitignore").write_text("/node_modules/\nfrontend/node_modules/\ntarget/\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "init")
    run_git(repo, "push", "-q", "origin", "HEAD:main")
    found = Host(
        tmp_path,
        repo,
        tmp_path / "worktrees",
        tmp_path / "targets",
        tmp_path / "cache",
        tmp_path / "ledger.txt",
    )
    for path in (found.worktrees, found.targets, found.cache):
        path.mkdir()
    (found.cache / "blob").write_bytes(b"x" * 8192)
    found.ledger.write_text("# tag pid=n model=m\nalive pid=100 model=a\ndead pid=200 model=b\n")
    return found


def judge(tags: tuple[str, ...] = (), used: tuple[Path, ...] = (), idle: float = 2.0) -> Judge:
    return Judge(frozenset(tags), tuple(path.resolve() for path in used), time.time(), idle)


# Small helpers --------------------------------------------------------------------------


def test_human_size_uses_binary_units() -> None:
    assert disk.human_size(512) == "512B"
    assert disk.human_size(1536) == "1.5K"
    assert disk.human_size(7 * 1024**3) == "7.0G"
    assert disk.human_size(3 * 1024**5) == "3072.0T"


def test_volume_percent_and_report_totals() -> None:
    assert Volume("/", 0, 0, 0).percent == 0
    full = Volume("/x", 100, 93, 7)
    report = DiskReport(
        (full, Volume("/y", 100, 10, 90)),
        (
            Item("cache", "/c", 30, True, "safe", "ok"),
            Item("target", "/t", 50, True, "safe", "ok"),
            Item("worktree", "/w", 99, False, "dirty", "1 change"),
        ),
        80,
        ("alive",),
    )
    assert report.full == (full,)
    assert report.total == 80
    assert list(report.reclaimable()) == ["target", "cache", "worktree", "build"]
    data = report.to_dict()
    assert data["reclaimable_total"] == 80
    assert data["volumes"] == [
        {"path": "/x", "total": 100, "used": 93, "free": 7, "percent": 93},
        {"path": "/y", "total": 100, "used": 10, "free": 90, "percent": 10},
    ]
    assert [item.path for item in report.kept] == ["/w"]


def test_tree_size_counts_only_what_deleting_frees(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "a").write_bytes(b"a" * 20000)
    os.link(tree / "sub" / "a", tree / "a-again")  # both links inside: freed
    (tmp_path / "outside").write_bytes(b"o" * 20000)
    os.link(tmp_path / "outside", tree / "shared")  # linked from outside: not freed
    (tree / "link").symlink_to(tmp_path / "outside")
    # "a" counts once (both its links go); "shared" not at all (it lives on outside).
    assert 20000 <= disk.tree_size(tree) < 40000
    os.unlink(tree / "sub" / "a")
    assert 20000 <= disk.tree_size(tree) < 40000  # "a-again" alone now, still counted
    assert disk.tree_size(tree / "a-again") >= 20000
    assert disk.tree_size(tmp_path / "missing") == 0
    assert disk.tree_size(tree / "shared") == 0


def test_tree_size_skips_unreadable_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "locked").mkdir()
    (tmp_path / "file").write_bytes(b"f" * 4096)
    real = os.scandir

    def scandir(path: str) -> object:
        if Path(path).name == "locked":
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert disk.tree_size(tmp_path) >= 4096


def test_tree_size_skips_entries_that_vanish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @dataclass
    class Gone:
        path: str

        def stat(self, follow_symlinks: bool = False) -> os.stat_result:
            raise FileNotFoundError(self.path)

    class Listing:
        def __enter__(self) -> list[Gone]:
            return [Gone(str(tmp_path / "gone"))]

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(os, "scandir", lambda path: Listing())
    assert disk.tree_size(tmp_path) > 0  # just the directory itself
    assert disk.last_activity([tmp_path]) > 0


def test_last_activity_reads_children_and_tolerates_missing(tmp_path: Path) -> None:
    (tmp_path / "child").write_text("x")
    age(tmp_path)
    os.utime(tmp_path / "child", (OLD + 100, OLD + 100))
    assert disk.last_activity([tmp_path, tmp_path / "missing"]) == pytest.approx(OLD + 100)
    assert disk.last_activity([]) == 0.0


def test_last_activity_ignores_unlistable_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    age(tmp_path)

    def denied(path: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "scandir", denied)
    assert disk.last_activity([tmp_path]) == pytest.approx(OLD)


def test_git_returns_none_when_git_cannot_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", broken)
    assert disk.git(Path("/"), "status") is None


def test_mount_point_walks_up_to_an_existing_mount(tmp_path: Path) -> None:
    point = disk.mount_point(tmp_path / "not" / "there")
    assert os.path.ismount(point)
    assert (tmp_path).is_relative_to(point)


def test_volumes_measure_configured_or_root_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = disk.volumes(DiskSettings(volumes=(str(tmp_path), str(tmp_path / "x"))))
    assert len(found) == 1 and found[0].total > 0
    default = disk.volumes(DiskSettings(worktree_roots=(str(tmp_path / "*" / "wt"),)))
    assert "/" in [volume.path for volume in default]
    assert disk.volumes(DiskSettings(caches=("*",)))[0].path == "/"

    def unavailable(path: object) -> None:
        raise OSError("gone")

    monkeypatch.setattr(shutil, "disk_usage", unavailable)
    assert disk.volumes(DiskSettings()) == ()


# Configuration --------------------------------------------------------------------------


def adopter(tmp_path: Path, body: str) -> Path:
    config = tmp_path / ".rotisserie" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(body)
    return config


def test_defaults_without_any_config(tmp_path: Path) -> None:
    env = {"HOME": "/home/u"}
    found = disk.load_settings(None, env, tmp_path)
    assert found.worktree_roots == (str(tmp_path / ".claude" / "worktrees"),)
    assert found.caches == disk.default_caches(env)
    assert found.warn_percent == 80 and found.source == ""
    with pytest.raises(DiskConfigError, match="no configuration file"):
        disk.load_settings(None, env, tmp_path, required=True)


def test_default_caches_follow_each_tools_own_variables() -> None:
    assert disk.default_caches({"HOME": "/h"}) == (
        "/h/.cache/uv",
        "/h/.cache/pip",
        "/h/.npm/_cacache",
        "/h/.cache/pnpm",
        "/h/.cache/go-build",
        "/h/.cargo/registry/cache",
        "/h/.cargo/registry/src",
    )
    moved = disk.default_caches(
        {
            "HOME": "/h",
            "XDG_CACHE_HOME": "/x",
            "CARGO_HOME": "/c",
            "npm_config_cache": "/n",
            "UV_CACHE_DIR": "/u",
            "PIP_CACHE_DIR": "/p",
            "GOCACHE": "/g",
        }
    )
    assert moved == (
        "/u",
        "/p",
        "/n/_cacache",
        "/x/pnpm",
        "/g",
        "/c/registry/cache",
        "/c/registry/src",
    )


def test_adopter_config_resolves_paths_from_the_repository(tmp_path: Path) -> None:
    config = adopter(
        tmp_path,
        "[disk]\n"
        'worktree_roots = [".claude/worktrees", "~/wt", "${SCRATCH}/w*"]\n'
        'target_roots = ["$SCRATCH/targets"]\n'
        'build_dirs = ["/abs/*/target"]\n'
        'caches = ["~"]\n'
        'volumes = ["/"]\n'
        'build_names = ["node_modules"]\n'
        'ledger = "factory/ledger.txt"\n'
        "warn_percent = 85\n"
        "idle_hours = 0\n",
    )
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert disk.find_config(deep) == config
    found = disk.load_settings(None, {"HOME": "/home/u", "SCRATCH": "/s"}, deep)
    assert found.worktree_roots == (str(tmp_path / ".claude/worktrees"), "/home/u/wt", "/s/w*")
    assert found.target_roots == ("/s/targets",)
    assert found.build_dirs == ("/abs/*/target",)
    assert found.caches == ("/home/u",)
    assert found.volumes == ("/",)
    assert found.build_names == ("node_modules",)
    assert found.ledger == str(tmp_path / "factory" / "ledger.txt")
    assert (found.warn_percent, found.idle_hours) == (85, 0.0)
    assert found.source == str(config)
    assert found.to_dict()["warn_percent"] == 85


def test_a_config_without_disk_keeps_the_defaults(tmp_path: Path) -> None:
    config = adopter(tmp_path, 'schema_version = 1\n[repository]\nowner = "o"\nname = "n"\n')
    found = disk.load_settings(config, {"HOME": "/h"}, Path("/"))
    assert found.worktree_roots == (str(tmp_path / ".claude" / "worktrees"),)
    assert found.target_roots == () and found.ledger == ""


def test_a_standalone_file_resolves_paths_next_to_itself(tmp_path: Path) -> None:
    config = tmp_path / "disk.toml"
    config.write_text('[disk]\ntarget_roots = ["targets"]\n')
    found = disk.load_settings(config, {}, Path("/"), required=True)
    assert found.target_roots == (str(tmp_path / "targets"),)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[disk\n", "not valid TOML"),
        ("disk = 3\n", "must be a table"),
        ("[disk]\nroots = []\n", "unknown \\[disk\\] setting"),
        ("[disk]\ncaches = 'x'\n", "caches must be a list of paths"),
        ("[disk]\ncaches = [1]\n", "caches must be a list of paths"),
        ("[disk]\nbuild_names = ['a/b']\n", "build_names"),
        ("[disk]\nledger = 3\n", "ledger must be a path"),
        ("[disk]\nwarn_percent = 100\n", "warn_percent"),
        ("[disk]\nwarn_percent = true\n", "warn_percent"),
        ("[disk]\nidle_hours = -1\n", "idle_hours"),
        ("[disk]\nidle_hours = 'x'\n", "idle_hours"),
        ("[disk]\ncaches = ['$NOPE/x']\n", "NOPE, which is not set"),
    ],
)
def test_bad_config_is_refused_with_the_reason(tmp_path: Path, body: str, message: str) -> None:
    config = tmp_path / "disk.toml"
    config.write_text(body)
    with pytest.raises(DiskConfigError, match=message):
        disk.load_settings(config, {}, tmp_path)


def test_an_unreadable_config_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DiskConfigError, match="cannot read"):
        disk.load_settings(tmp_path, {}, tmp_path)


# Processes ------------------------------------------------------------------------------


def test_proc_processes_reads_cwd_open_files_and_target_dir(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    entry = proc / "123"
    (entry / "fd").mkdir(parents=True)
    (entry / "cwd").symlink_to("/work/tree")
    (entry / "root").symlink_to("/")
    (entry / "fd" / "3").symlink_to("/cache/file (deleted)")
    (entry / "fd" / "4").symlink_to("socket:[1]")
    (entry / "environ").write_bytes(b"CARGO_TARGET_DIR=/targets/t1\0PATH=/bin\0CARGO_TARGET_DIR\0")
    other = proc / "456"
    other.mkdir()  # a process we may not inspect: nothing readable
    (proc / "self").mkdir()
    found = ProcProcesses(proc).paths_in_use()
    assert found == (Path("/cache/file"), Path("/targets/t1"), Path("/work/tree"))
    assert ProcProcesses(tmp_path / "missing").paths_in_use() == ()


def test_proc_processes_reads_this_host() -> None:
    processes = ProcProcesses()
    assert processes.alive(os.getpid())
    assert not processes.alive(2**70)
    child = subprocess.Popen(["true"])
    child.wait()
    assert not processes.alive(child.pid)
    assert processes.alive(1)  # someone else's process is alive even if not ours
    if Path("/proc").is_dir():
        assert Path.cwd().resolve() in processes.paths_in_use()


def test_live_tags_reads_the_ledger(host: Host) -> None:
    processes = FakeProcesses({100})
    assert disk.live_tags(str(host.ledger), processes) == ("alive",)
    assert disk.live_tags("", processes) == ()
    assert disk.live_tags(str(host.base / "missing"), processes) == ()


# Judging --------------------------------------------------------------------------------


def test_a_clean_pushed_idle_worktree_is_safe(host: Host) -> None:
    path = host.worktree("done")
    age(host.base)
    found = judge().worktree(path)
    assert found.safe and found.code == "safe", found
    assert "origin/done" in found.reason
    assert Path(found.repository) == (host.repo / ".git").resolve()


def test_a_worktree_merged_into_main_counts_as_pushed(host: Host) -> None:
    path = host.worktree("merged", push=False)
    run_git(path, "push", "-q", "origin", "HEAD:main")
    age(host.base)
    assert judge().worktree(path).code == "safe"


def git_dir(path: Path) -> Path:
    return Path(run_git(path, "rev-parse", "--absolute-git-dir").strip())


def staged(path: Path) -> None:
    (path / "wip.txt").write_text("x")
    run_git(path, "add", "wip.txt")


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda h, p: None, "live-worker"),
        (lambda h, p: (p / "new.txt").write_text("x"), "dirty"),
        (lambda h, p: staged(p), "dirty"),
        (lambda h, p: run_git(h.repo, "worktree", "lock", str(p)), "locked"),
        (lambda h, p: (git_dir(p) / "MERGE_HEAD").write_text("x"), "in-progress"),
    ],
)
def test_worktrees_with_unfinished_work_are_kept(
    host: Host, setup: Callable[[Host, Path], object], code: str
) -> None:
    path = host.worktree("busy")
    setup(host, path)
    age(host.base)
    tags = ("busy",) if code == "live-worker" else ()
    found = judge(tags=tags).worktree(path)
    assert (found.safe, found.code) == (False, code)


def test_an_unpushed_worktree_is_kept(host: Host) -> None:
    path = host.worktree("local", push=False)
    age(host.base)
    assert judge().worktree(path).code == "unpushed"


def test_a_recently_touched_worktree_is_kept(host: Host) -> None:
    path = host.worktree("fresh")
    found = judge().worktree(path)
    assert (found.code, found.reason) == ("recent", "changed in the last 2h")
    assert judge(idle=0).worktree(path).safe


def test_a_worktree_a_process_uses_is_kept(host: Host) -> None:
    path = host.worktree("used")
    age(host.base)
    assert judge(used=(path / "sub",)).worktree(path).code == "in-use"
    assert judge(used=(host.worktrees / "used-other",)).worktree(path).safe


def test_non_git_and_broken_checkouts_are_left_alone(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = host.worktrees / "evidence"
    evidence.mkdir()
    assert judge().worktree(evidence).code == "not-git"
    broken = host.worktrees / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere\n")
    assert judge().worktree(broken).code == "git-error"
    empty = host.worktrees / "empty"
    run_git(host.base, "init", "-q", str(empty))
    age(host.base)
    assert judge().worktree(empty).code == "no-commits"
    path = host.worktree("status-fails")
    age(host.base)
    real = disk.git

    def flaky(where: Path, *args: str) -> str | None:
        return None if args[0] == "status" else real(where, *args)

    monkeypatch.setattr(disk, "git", flaky)
    assert judge().worktree(path).code == "git-error"


def test_targets_outputs_and_caches(tmp_path: Path) -> None:
    target = tmp_path / "t1"
    target.mkdir()
    age(target)
    assert judge(tags=("t1",)).target(target).code == "live-worker"
    assert judge().target(target).reason == "no live worker named t1"
    assert judge(used=(target,)).target(target).code == "in-use"
    os.utime(target)
    assert judge().target(target).code == "recent"
    nested = tmp_path / "wt" / "frontend" / "node_modules"
    nested.mkdir(parents=True)
    age(nested)
    assert judge(tags=("wt",)).output(nested).reason == "worker wt is still running"
    cache = tmp_path / "cache"
    cache.mkdir()
    assert judge().cache(cache).safe
    assert judge(used=(cache / "lock",)).cache(cache).code == "in-use"


# Scanning and removing -------------------------------------------------------------------


def build_host(host: Host) -> dict[str, Path]:
    """One of everything: safe, kept, and kept-but-with-reclaimable outputs."""
    done = host.worktree("done")
    (done / "node_modules").mkdir()
    (done / "node_modules" / "pkg.js").write_bytes(b"p" * 4096)
    dirty = host.worktree("dirty")
    (dirty / "notes.txt").write_text("unsaved")
    modules = dirty / "frontend" / "node_modules"
    modules.mkdir(parents=True)
    (modules / "big.js").write_bytes(b"m" * 50000)
    (dirty / "target").mkdir()
    (dirty / "target" / "out").write_bytes(b"t" * 4096)
    (dirty / "vendor" / "node_modules").mkdir(parents=True)  # not ignored: part of the change
    live = host.worktree("alive")
    dead_target = host.targets / "dead"
    dead_target.mkdir()
    (dead_target / "debug").write_bytes(b"d" * 30000)
    live_target = host.targets / "alive"
    live_target.mkdir()
    built = host.base / "code" / "app" / "target"
    built.mkdir()
    (built / "release").write_bytes(b"r" * 4096)
    (host.worktrees / "stray-file").write_text("not a directory")
    age(host.base)
    return {
        "done": done,
        "dirty": dirty,
        "modules": modules,
        "live": live,
        "dead_target": dead_target,
        "live_target": live_target,
        "built": built,
    }


def test_scan_reports_safe_and_kept_items_with_sizes(host: Host) -> None:
    paths = build_host(host)
    scanner = Scanner(host.settings(), FakeProcesses({100}), keep=("extra",))
    report = scanner.scan()
    safe = {(item.category, Path(item.path)) for item in report.safe}
    assert safe == {
        ("worktree", paths["done"]),
        ("target", paths["dead_target"]),
        ("build", paths["built"]),
        ("build", paths["modules"]),
        ("build", paths["dirty"] / "target"),
        ("cache", host.cache),
    }
    kept = {Path(item.path): item.code for item in report.kept}
    assert kept == {
        paths["dirty"]: "dirty",
        paths["live"]: "live-worker",
        paths["live_target"]: "live-worker",
    }
    assert report.live_tags == ("alive", "extra")
    sizes = [item.size for item in report.safe]
    assert sizes == sorted(sizes, reverse=True)
    assert report.reclaimable()["build"] >= 50000
    assert report.volumes[0].total > 0


def test_remove_deletes_only_safe_items_and_prunes_worktrees(host: Host) -> None:
    paths = build_host(host)
    processes = FakeProcesses({100})
    scanner = Scanner(host.settings(), processes)
    report = scanner.scan()
    removed = scanner.remove(report)
    assert all(change.ok for change in removed), removed
    assert {Path(change.path) for change in removed} == {Path(item.path) for item in report.safe}
    assert not paths["done"].exists() and not paths["dead_target"].exists()
    assert not paths["modules"].exists() and not host.cache.exists()
    assert paths["dirty"].exists() and (paths["dirty"] / "notes.txt").exists()
    assert paths["live"].exists() and paths["live_target"].exists()
    listed = run_git(host.repo, "worktree", "list")
    assert "done" not in listed and "dirty" in listed
    assert removed[0].to_dict()["ok"] is True


def test_remove_rechecks_each_item_before_deleting(host: Host) -> None:
    paths = build_host(host)
    processes = FakeProcesses({100})
    scanner = Scanner(host.settings(), processes)
    report = scanner.scan()
    processes.used.append(paths["dead_target"])  # a worker started using it since
    (paths["dirty"] / "notes.txt").unlink()  # its worktree became clean but unpushed
    run_git(paths["dirty"], "commit", "-q", "--allow-empty", "-m", "more")
    age(host.base)
    shutil.rmtree(host.cache)  # someone else cleaned it
    removed = {Path(change.path): change for change in scanner.remove(report)}
    assert removed[paths["dead_target"]].text == "kept: a running process uses it"
    assert removed[host.cache].text == "already gone"
    assert removed[paths["modules"]].ok  # unpushed is still idle: outputs may go
    assert paths["dead_target"].exists()


def test_remove_keeps_outputs_of_a_worktree_that_became_active(host: Host) -> None:
    paths = build_host(host)
    scanner = Scanner(host.settings(), FakeProcesses({100}))
    report = scanner.scan()
    os.utime(paths["dirty"])  # a worker came back to it
    removed = {Path(change.path): change for change in scanner.remove(report)}
    assert removed[paths["modules"]].text == "kept: its worktree: changed in the last 2h"
    assert paths["modules"].exists()


def test_remove_refuses_anything_outside_the_configured_roots(host: Host) -> None:
    outside = host.base / "precious"
    outside.mkdir()
    (host.worktrees / "link").symlink_to(outside)
    scanner = Scanner(host.settings(), FakeProcesses())
    forged = DiskReport(
        (),
        (
            Item("cache", str(outside), 1, True, "safe", "forged"),
            Item("build", str(host.worktrees / "link"), 1, True, "safe", "forged"),
            Item("build", str(outside), 1, True, "safe", "forged"),
        ),
        80,
    )
    removed = scanner.remove(forged)
    assert [change.text for change in removed] == ["outside the configured roots"] * 3
    assert outside.exists()


def test_remove_reports_a_partial_deletion(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    target = host.targets / "dead"
    target.mkdir()
    age(target)
    scanner = Scanner(host.settings(caches=()), FakeProcesses())
    report = scanner.scan()

    def stubborn(path: Path, onexc: Callable[[object, str, BaseException], object]) -> None:
        onexc(os.rmdir, str(path), PermissionError("busy"))

    monkeypatch.setattr(shutil, "rmtree", stubborn)
    removed = scanner.remove(report)
    assert removed[0].text.startswith("partly removed (1 error(s))")
    assert not removed[0].ok


def test_unlistable_roots_yield_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()

    def denied(self: Path) -> Iterator[Path]:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    assert disk._children(root) == []


# Doctor -------------------------------------------------------------------------------


def test_doctor_warns_above_the_threshold_and_points_at_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measured: dict[str, tuple[Volume, ...]] = {
        "volumes": (Volume("/data", 100, 93, 7 * 1024**3), Volume("/", 100, 50, 50))
    }
    monkeypatch.setattr(disk, "volumes", lambda settings: measured["volumes"])
    adopter(tmp_path, "[disk]\nwarn_percent = 80\n")
    found = doctor.disk_space(tmp_path, {"HOME": str(tmp_path)})
    assert (found.status, found.text) == ("warn", "/data is 93% full (7.0G free)")
    assert "rotisserie factory disk" in found.hint
    measured["volumes"] = (Volume("/", 100, 80, 20),)
    calm = doctor.disk_space(tmp_path, {"HOME": str(tmp_path)})
    assert (calm.status, calm.text) == ("ok", "1 volume(s) at or under 80% full")
    measured["volumes"] = ()
    blind = doctor.disk_space(tmp_path, {"HOME": str(tmp_path)})
    assert (blind.status, blind.text) == ("warn", "could not measure any volume")
    adopter(tmp_path, "[disk]\nnope = 1\n")
    assert doctor.disk_space(tmp_path, {}).text.startswith("could not check: unknown")
    assert doctor.disk_space().check == "disk space"


# CLI ----------------------------------------------------------------------------------


@pytest.fixture
def cli_host(host: Host, monkeypatch: pytest.MonkeyPatch) -> tuple[Host, Path]:
    build_host(host)
    config = host.base / "disk.toml"
    settings = host.settings()
    lines = ["[disk]"]
    for key in ("worktree_roots", "target_roots", "build_dirs", "caches", "volumes"):
        lines.append(f"{key} = {json.dumps(list(getattr(settings, key)))}")
    lines.append(f"ledger = {json.dumps(settings.ledger)}")
    config.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(disk, "ProcProcesses", lambda: FakeProcesses({100}))
    monkeypatch.chdir(host.base)
    return host, config


def run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    arguments = cli.parser().parse_args(["factory", *argv])
    out = io.StringIO()
    code = commands.main(arguments, env=env or {"TERM": "dumb"}, stdout=out, executable="sh")
    return code, out.getvalue()


def test_cli_reports_without_deleting(cli_host: tuple[Host, Path]) -> None:
    host, config = cli_host
    code, out = run(["disk", "--config", str(config)])
    assert code == 0
    assert "Disk usage" in out and "Safe to reclaim:" in out
    assert f"Remove them: rotisserie factory disk --config {config} --fix --yes" in out
    assert "Kept: 3 item(s)" in out and "worker alive is still running" in out
    assert (host.worktrees / "done").exists()
    code, out = run(["disk", "--config", str(config), "--fix"])
    assert f"Dry run: rotisserie factory disk --config {config} --fix --yes would delete 6" in out
    assert (host.worktrees / "done").exists() and host.cache.exists()


def test_cli_lists_every_kept_item_with_all(
    cli_host: tuple[Host, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, config = cli_host
    monkeypatch.setattr(commands, "KEPT_SHOWN", 1)
    code, out = run(["disk", "--config", str(config)])
    assert "... and 2 more (--all lists them)" in out
    code, out = run(["disk", "--config", str(config), "--all"])
    assert "more (--all lists them)" not in out


def test_cli_fix_yes_deletes_the_safe_items(cli_host: tuple[Host, Path]) -> None:
    host, config = cli_host
    code, out = run(["disk", "--config", str(config), "--fix", "--yes", "--json"])
    data = json.loads(out)
    assert code == 0 and data["result"] == "completed"
    assert data["freed"] > 0 and len(data["removed"]) == 6
    assert data["settings"]["source"] == str(config)
    assert not (host.worktrees / "done").exists()
    assert (host.worktrees / "dirty" / "notes.txt").exists()
    assert "repository" not in data  # no adopter repository was needed
    code, out = run(["disk", "--config", str(config), "--fix", "--yes"])
    assert code == 0 and "Freed about 0B." in out
    assert "Safe to reclaim: " in out


def test_cli_fix_yes_reports_failures(
    cli_host: tuple[Host, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, config = cli_host

    def stubborn(path: Path, onexc: Callable[[object, str, BaseException], object]) -> None:
        onexc(os.rmdir, str(path), PermissionError("busy"))

    monkeypatch.setattr(shutil, "rmtree", stubborn)
    code, out = run(["disk", "--config", str(config), "--fix", "--yes"])
    assert code == 4 and "partly removed" in out


def test_cli_rejects_a_bad_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, out = run(["disk", "--config", str(tmp_path / "missing.toml")])
    assert code == 2 and "cannot read" in out


def test_cli_with_nothing_to_reclaim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "disk.toml"
    config.write_text(f'[disk]\ncaches = []\nvolumes = ["{tmp_path}"]\nwarn_percent = 1\n')
    monkeypatch.setattr(disk, "ProcProcesses", lambda: FakeProcesses())
    monkeypatch.chdir(tmp_path)
    code, out = run(["disk", "--config", str(config), "--repo", "o/n"])
    assert code == 0 and "Safe to reclaim: 0B" in out
    assert "% used" in out and "Remove them" not in out and "Kept:" not in out


def test_other_commands_still_need_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out = run(["status"])
    assert code == 2 and "Could not tell which repository" in out


def test_a_checkout_without_build_outputs_yields_none(tmp_path: Path) -> None:
    assert disk._ignored_outputs(tmp_path, disk.DEFAULT_BUILD_NAMES) == []


# Review follow-ups -----------------------------------------------------------------------


def test_the_suggested_command_keeps_the_config_and_kept_tags(
    cli_host: tuple[Host, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    host, config = cli_host
    monkeypatch.chdir(host.base)
    spaced = host.base / "my disk.toml"
    spaced.write_text(config.read_text())
    code, out = run(["disk", "--config", "my disk.toml", "--keep", "done", "--keep", "a b"])
    assert code == 0
    assert f"Remove them: rotisserie factory disk --config '{spaced}' --keep done " in out
    assert "--keep 'a b' --fix --yes" in out
    assert "worker done is still running" in out


def test_git_ignores_inherited_repository_overrides(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = host.worktree("leaky")
    (path / "unsaved.txt").write_text("x")
    age(host.base)
    # A clean checkout's GIT_DIR must not vouch for the dirty candidate.
    monkeypatch.setenv("GIT_DIR", str(host.repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(host.repo))
    monkeypatch.setenv("GIT_INDEX_FILE", str(host.repo / ".git" / "index"))
    assert judge().worktree(path).code == "dirty"
    assert "GIT_DIR" not in disk.git_environment(os.environ)
    assert disk.git_environment({"HOME": "/h", "GIT_WORK_TREE": "/w"}) == {"HOME": "/h"}


def test_symlinks_below_a_root_never_steer_a_deletion(host: Host) -> None:
    valuable = host.base / "valuable"
    (valuable / "target").mkdir(parents=True)
    (valuable / "target" / "keep.me").write_text("precious")
    projects = host.base / "projects"
    (projects / "real" / "target").mkdir(parents=True)
    (projects / "evil").symlink_to(valuable)
    age(host.base)
    pattern = str(projects / "*" / "target")
    assert disk._matches([pattern]) == [projects / "real" / "target"]
    assert disk.contained(projects / "real" / "target", projects)
    assert not disk.contained(projects / "evil" / "target", projects)
    assert not disk.contained(valuable, projects)
    scanner = Scanner(host.settings(build_dirs=(pattern,), caches=()), FakeProcesses())
    report = scanner.scan()
    assert [item.path for item in report.safe] == [str(projects / "real" / "target")]
    forged = DiskReport(
        (), (Item("build", str(projects / "evil" / "target"), 1, True, "safe", "forged"),), 80
    )
    assert scanner.remove(forged)[0].text == "outside the configured roots"
    assert (valuable / "target" / "keep.me").exists()


def test_symlinked_folders_inside_a_worktree_are_not_build_outputs(host: Host) -> None:
    path = host.worktree("linky")
    (path / "notes.txt").write_text("unsaved")
    elsewhere = host.base / "elsewhere"
    (elsewhere / "node_modules").mkdir(parents=True)
    (path / "frontend").symlink_to(elsewhere)
    (path / "node_modules").mkdir()
    age(host.base)
    outputs = disk._ignored_outputs(path, ("node_modules",))
    assert outputs == [path / "node_modules"]
    scanner = Scanner(host.settings(caches=()), FakeProcesses())
    forged = DiskReport(
        (), (Item("build", str(path / "frontend" / "node_modules"), 1, True, "safe", "x"),), 80
    )
    assert scanner.remove(forged)[0].text == "outside the configured roots"
    assert (elsewhere / "node_modules").exists()


def test_nothing_with_a_filesystem_mounted_inside_is_deleted(host: Host) -> None:
    (host.cache / "shared").mkdir()
    age(host.base)
    mounts: dict[str, tuple[Path, ...] | None] = {"now": (Path("/"), host.cache / "shared")}
    scanner = Scanner(
        host.settings(worktree_roots=()), FakeProcesses(), mounts=lambda: mounts["now"]
    )
    kept = scanner.scan().kept[0]
    assert (kept.code, kept.reason) == (
        "mounted",
        f"a filesystem is mounted at {host.cache}/shared",
    )
    mounts["now"] = None
    assert scanner.scan().kept[0].reason == "cannot tell what is mounted inside it"
    mounts["now"] = (Path("/"),)
    report = scanner.scan()
    assert report.safe[0].path == str(host.cache)
    mounts["now"] = (host.cache / "shared",)  # mounted between the scan and the fix
    removed = scanner.remove(report)
    assert removed[0].text == f"kept: a filesystem is mounted at {host.cache}/shared"
    assert (host.cache / "blob").exists()


def test_mount_points_reads_mountinfo(tmp_path: Path) -> None:
    info = tmp_path / "mountinfo"
    info.write_text(
        "22 1 259:6 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
        "40 22 259:8 /data /mnt/my\\040disk rw shared:2 - ext4 /dev/x rw\n"
        "short line\n"
    )
    assert disk.mount_points(info) == (Path("/"), Path("/mnt/my disk"))
    assert disk.mount_points(tmp_path / "missing") is None
    assert Path("/") in (disk.mount_points() or (Path("/"),))


@dataclass
class ChangingProcesses:
    """No process uses anything until ``after`` calls; then ``later`` is in use."""

    later: Path
    after: int
    calls: int = 0

    def alive(self, pid: int) -> bool:
        return False

    def paths_in_use(self) -> tuple[Path, ...]:
        self.calls += 1
        return (self.later.resolve(),) if self.calls > self.after else ()


def test_process_use_is_checked_again_before_each_deletion(host: Host) -> None:
    big, small = host.base / "big-cache", host.base / "small-cache"
    for path, size in ((big, 90000), (small, 9000)):
        path.mkdir()
        (path / "blob").write_bytes(b"x" * size)
    processes = ChangingProcesses(small, after=3)  # scan, remove's first look, the first deletion
    scanner = Scanner(host.settings(worktree_roots=(), caches=(str(big), str(small))), processes)
    report = scanner.scan()
    assert [item.path for item in report.safe] == [str(big), str(small)]
    removed = scanner.remove(report)
    assert [change.text for change in removed] == ["removed", "kept: a running process uses it"]
    assert small.exists() and not big.exists()


def test_a_recent_change_deep_inside_keeps_a_target(tmp_path: Path) -> None:
    deep = tmp_path / "t1" / "debug" / "incremental" / "session"
    deep.mkdir(parents=True)
    (deep / "file").write_text("x")
    age(tmp_path)
    assert judge().target(tmp_path / "t1").safe
    os.utime(deep / "file")  # rewritten in place: no ancestor mtime changes
    assert judge().target(tmp_path / "t1").code == "recent"


def test_walk_stays_on_one_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class Info:
        st_mode: int
        st_dev: int
        st_mtime: float = 0.0

    @dataclass
    class Entry:
        path: str
        info: Info

        def stat(self, follow_symlinks: bool = False) -> Info:
            return self.info

    device = tmp_path.lstat().st_dev
    listings = {
        str(tmp_path): [
            Entry(str(tmp_path / "same"), Info(0o040755, device)),
            Entry(str(tmp_path / "other"), Info(0o040755, device + 1)),
        ],
        str(tmp_path / "same"): [Entry(str(tmp_path / "same" / "f"), Info(0o100644, device))],
    }
    opened: list[str] = []

    class Listing:
        def __init__(self, path: str) -> None:
            opened.append(str(path))
            self.entries = listings.get(str(path), [])

        def __enter__(self) -> list[Entry]:
            return self.entries

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(os, "scandir", Listing)
    assert len(list(disk.walk(tmp_path))) == 3
    assert opened == [str(tmp_path), str(tmp_path / "same")]
    assert list(disk.walk(tmp_path / "missing")) == []


def test_a_safe_item_inside_a_safe_worktree_is_counted_once(host: Host) -> None:
    done = host.worktree("done")
    (done / "target").mkdir()
    (done / "target" / "out").write_bytes(b"t" * 40000)
    age(host.base)
    pattern = str(host.worktrees / "*" / "target")
    report = Scanner(host.settings(build_dirs=(pattern,), caches=()), FakeProcesses()).scan()
    assert [(item.category, item.path) for item in report.safe] == [("worktree", str(done))]
    assert report.total == report.safe[0].size >= 40000


def test_a_config_that_is_not_utf8_is_a_config_error(tmp_path: Path) -> None:
    config = adopter(tmp_path, "")
    config.write_bytes(b"[disk]\nledger = '\xff'\n")
    with pytest.raises(DiskConfigError, match="is not UTF-8 text"):
        disk.load_settings(config, {}, tmp_path)
    found = doctor.disk_space(tmp_path, {})
    assert found.status == "warn" and "not UTF-8" in found.text
