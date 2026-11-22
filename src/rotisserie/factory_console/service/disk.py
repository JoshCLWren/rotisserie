"""Where did the disk go, and what can the factory safely take back?

A factory host fills its disks with leftovers: worktrees of workers that
finished long ago, Rust ``target`` directories named after dead worker tags,
``node_modules`` in abandoned checkouts, and package caches. ``scan`` measures
each configured volume and every reclaimable item under the configured roots;
``remove`` deletes only the items ``scan`` judged safe, re-checking each one
first. Nothing here prints; the CLI renders the typed results.

What counts as safe (everything else is kept, with the reason):

* a **worktree** with no uncommitted or untracked changes, no merge or rebase in
  progress, not locked, whose ``HEAD`` is on a remote branch (pushed or merged),
  that no live worker tag owns, that no running process uses, and that has been
  idle for ``idle_hours``;
* a **target** directory (``CARGO_TARGET_DIR`` per worker tag) whose tag has no
  live worker, that no running process uses, idle for ``idle_hours``;
* a **build** output (configured ``build_dirs``, plus git-ignored
  ``build_names`` inside a kept worktree that has no live owner) that no running
  process uses, idle for ``idle_hours``;
* a **cache** (regenerable package caches) that no running process uses.

Nothing outside the configured roots is ever deleted. Sizes count allocated
blocks and only the files the deletion would actually free: a file hard-linked
from elsewhere (``uv`` caches link into virtualenvs) frees nothing.

Configuration lives in the adopter's ``.rotisserie/config.toml`` (or any TOML
file passed with ``--config``) under ``[disk]``; see ``docs/factory-console.md``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

CATEGORIES = ("worktree", "target", "build", "cache")
DEFAULT_WARN_PERCENT = 80
DEFAULT_IDLE_HOURS = 2.0
DEFAULT_BUILD_NAMES = ("node_modules", "target")
# A merge, rebase, cherry-pick, revert or bisect in progress means unfinished work.
IN_PROGRESS = (
    "MERGE_HEAD",
    "rebase-merge",
    "rebase-apply",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
)
KEYS = {
    "warn_percent",
    "idle_hours",
    "volumes",
    "worktree_roots",
    "target_roots",
    "build_dirs",
    "build_names",
    "caches",
    "ledger",
}
# A kept worktree in one of these states has no owner at work in it, so its
# git-ignored build outputs may go (a recent, locked or live one keeps them).
IDLE_KEPT = ("dirty", "unpushed", "in-progress", "no-commits")
LEDGER_LINE = re.compile(r"^(?P<tag>\S+)\s+pid=(?P<pid>\d+)\b")
VARIABLE = re.compile(r"\$(?:\{(?P<braced>\w+)\}|(?P<plain>\w+))")
GIT_TIMEOUT = 30
# Inherited variables that point git at a different repository than ``-C`` names.
# A clean GIT_DIR must never vouch for a dirty candidate.
GIT_LOCATION_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
MOUNTINFO = Path("/proc/self/mountinfo")
ESCAPE = re.compile(r"\\([0-7]{3})")


class DiskConfigError(ValueError):
    """The ``[disk]`` configuration cannot work as written."""


@dataclass(frozen=True)
class DiskSettings:
    """Which volumes to watch and which roots the factory may clean."""

    worktree_roots: tuple[str, ...] = ()  # glob patterns; each child is a checkout
    target_roots: tuple[str, ...] = ()  # glob patterns; each child is a tag's target dir
    build_dirs: tuple[str, ...] = ()  # glob patterns; each match is a build output
    caches: tuple[str, ...] = ()  # regenerable cache directories
    volumes: tuple[str, ...] = ()  # empty: "/" plus every root's mount point
    build_names: tuple[str, ...] = DEFAULT_BUILD_NAMES
    ledger: str = ""  # "<tag> pid=<n> ..." lines; a live pid keeps the tag's work
    warn_percent: int = DEFAULT_WARN_PERCENT
    idle_hours: float = DEFAULT_IDLE_HOURS
    source: str = ""  # the config file these came from, if any

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Volume:
    path: str
    total: int
    used: int
    free: int

    @property
    def percent(self) -> int:
        return round(100 * self.used / self.total) if self.total else 0

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "percent": self.percent}


@dataclass(frozen=True)
class Item:
    """One thing the factory could delete, and whether it is safe to."""

    category: str  # worktree | target | build | cache
    path: str
    size: int  # bytes the deletion would free
    safe: bool
    code: str  # safe | live-worker | in-use | dirty | unpushed | in-progress | locked |
    # recent | not-git | git-error | no-commits | outside-roots | missing
    reason: str
    repository: str = ""  # a worktree's git common dir, pruned after removal

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DiskReport:
    volumes: tuple[Volume, ...]
    items: tuple[Item, ...]
    warn_percent: int
    live_tags: tuple[str, ...] = ()

    @property
    def safe(self) -> tuple[Item, ...]:
        return tuple(item for item in self.items if item.safe)

    @property
    def kept(self) -> tuple[Item, ...]:
        return tuple(item for item in self.items if not item.safe)

    @property
    def full(self) -> tuple[Volume, ...]:
        return tuple(volume for volume in self.volumes if volume.percent > self.warn_percent)

    def reclaimable(self) -> dict[str, int]:
        """Bytes safe to free, per category (every category present, largest first)."""
        totals = {category: 0 for category in CATEGORIES}
        for item in self.safe:
            totals[item.category] += item.size
        return dict(sorted(totals.items(), key=lambda pair: -pair[1]))

    @property
    def total(self) -> int:
        return sum(item.size for item in self.safe)

    def to_dict(self) -> dict[str, object]:
        return {
            "volumes": [volume.to_dict() for volume in self.volumes],
            "items": [item.to_dict() for item in self.items],
            "warn_percent": self.warn_percent,
            "live_tags": list(self.live_tags),
            "reclaimable": self.reclaimable(),
            "reclaimable_total": self.total,
        }


@dataclass(frozen=True)
class Removed:
    category: str
    path: str
    size: int
    ok: bool
    text: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def human_size(size: int) -> str:
    """1536 -> "1.5K"; binary units, like ``df -h``."""
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


# Processes ----------------------------------------------------------------------------


class Processes(Protocol):
    """What is running on this host: which pids live and which paths they hold."""

    def alive(self, pid: int) -> bool: ...

    def paths_in_use(self) -> tuple[Path, ...]: ...


@dataclass
class ProcProcesses:
    """Reads ``/proc``: each process's working directory, open files and target dir."""

    root: Path = Path("/proc")
    environment_keys: tuple[str, ...] = ("CARGO_TARGET_DIR",)

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OverflowError:
            return False
        return True

    def paths_in_use(self) -> tuple[Path, ...]:
        found: set[Path] = set()
        try:
            entries = [entry for entry in self.root.iterdir() if entry.name.isdigit()]
        except OSError:
            return ()
        for entry in entries:
            found.update(self._process_paths(entry))
        return tuple(sorted(found))

    def _process_paths(self, entry: Path) -> set[Path]:
        found: set[Path] = set()
        for link in (entry / "cwd", entry / "root"):
            target = _readlink(link)
            if target and target != "/":
                found.add(Path(target))
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:
            descriptors = []
        for descriptor in descriptors:
            target = _readlink(descriptor)
            if target.startswith("/"):
                found.add(Path(target.removesuffix(" (deleted)")))
        try:
            environment = (entry / "environ").read_bytes()
        except OSError:
            environment = b""
        for pair in environment.split(b"\0"):
            key, _, value = pair.decode("utf-8", "replace").partition("=")
            if key in self.environment_keys and value.startswith("/"):
                found.add(Path(value))
        return found


def _readlink(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def live_tags(ledger: str, processes: Processes) -> tuple[str, ...]:
    """Tags in the local factory ledger whose worker process is still running."""
    if not ledger:
        return ()
    try:
        lines = Path(ledger).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    tags: set[str] = set()
    for line in lines:
        match = LEDGER_LINE.match(line.strip())
        if match and processes.alive(int(match.group("pid"))):
            tags.add(match.group("tag"))
    return tuple(sorted(tags))


# Configuration ------------------------------------------------------------------------


def find_config(cwd: Path) -> Path | None:
    """The nearest ``.rotisserie/config.toml`` at or above ``cwd``."""
    for directory in (cwd, *cwd.parents):
        config = directory / ".rotisserie" / "config.toml"
        if config.is_file():
            return config
    return None


def default_caches(env: Mapping[str, str]) -> tuple[str, ...]:
    """Regenerable package caches, located the way each tool locates its own."""
    home = env.get("HOME", str(Path.home()))
    cache = env.get("XDG_CACHE_HOME") or f"{home}/.cache"
    cargo = env.get("CARGO_HOME") or f"{home}/.cargo"
    npm = env.get("npm_config_cache") or env.get("NPM_CONFIG_CACHE") or f"{home}/.npm"
    return (
        env.get("UV_CACHE_DIR") or f"{cache}/uv",
        env.get("PIP_CACHE_DIR") or f"{cache}/pip",
        f"{npm}/_cacache",
        f"{cache}/pnpm",
        env.get("GOCACHE") or f"{cache}/go-build",
        f"{cargo}/registry/cache",
        f"{cargo}/registry/src",
    )


def load_settings(
    config: Path | None, env: Mapping[str, str], cwd: Path, *, required: bool = False
) -> DiskSettings:
    """Read ``[disk]`` from ``config`` (or the nearest adopter config) over the defaults.

    Relative paths resolve against the adopter repository (the directory holding
    ``.rotisserie``), or the config file's directory for a standalone file. ``~``
    and ``$VARIABLE`` expand from ``env``.
    """
    path = config or find_config(cwd)
    if path is None:
        if required:
            raise DiskConfigError(f"no configuration file at {config}")
        return _defaults(cwd, env, "")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DiskConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise DiskConfigError(f"{path} is not UTF-8 text: {exc.reason}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise DiskConfigError(f"{path} is not valid TOML: {exc}") from exc
    base = path.parent.parent if path.parent.name == ".rotisserie" else path.parent
    section = data.get("disk", {})
    if not isinstance(section, dict):
        raise DiskConfigError(f"[disk] in {path} must be a table")
    unknown = sorted(set(section) - KEYS)
    if unknown:
        raise DiskConfigError(f"unknown [disk] setting(s) in {path}: {', '.join(unknown)}")
    defaults = _defaults(base, env, str(path))

    def paths(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        if key not in section:
            return fallback
        value = section[key]
        if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
            raise DiskConfigError(f"[disk] {key} must be a list of paths")
        return tuple(_expand(part, env, base) for part in value)

    names = section.get("build_names", list(DEFAULT_BUILD_NAMES))
    if not isinstance(names, list) or not all(
        isinstance(name, str) and name and "/" not in name for name in names
    ):
        raise DiskConfigError("[disk] build_names must be a list of directory names")
    ledger = section.get("ledger", "")
    if not isinstance(ledger, str):
        raise DiskConfigError("[disk] ledger must be a path")
    warn = section.get("warn_percent", DEFAULT_WARN_PERCENT)
    if isinstance(warn, bool) or not isinstance(warn, int) or not 1 <= warn <= 99:
        raise DiskConfigError("[disk] warn_percent must be a whole number from 1 to 99")
    idle = section.get("idle_hours", DEFAULT_IDLE_HOURS)
    if isinstance(idle, bool) or not isinstance(idle, int | float) or idle < 0:
        raise DiskConfigError("[disk] idle_hours must be a number of hours, 0 or more")
    return DiskSettings(
        worktree_roots=paths("worktree_roots", defaults.worktree_roots),
        target_roots=paths("target_roots", ()),
        build_dirs=paths("build_dirs", ()),
        caches=paths("caches", defaults.caches),
        volumes=paths("volumes", ()),
        build_names=tuple(names),
        ledger=_expand(ledger, env, base) if ledger else "",
        warn_percent=warn,
        idle_hours=float(idle),
        source=str(path),
    )


def _defaults(base: Path, env: Mapping[str, str], source: str) -> DiskSettings:
    return DiskSettings(
        worktree_roots=(str(base / ".claude" / "worktrees"),),
        caches=default_caches(env),
        source=source,
    )


def _expand(value: str, env: Mapping[str, str], base: Path) -> str:
    def variable(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain")
        if name not in env:
            raise DiskConfigError(f"[disk] path {value!r} uses ${name}, which is not set")
        return env[name]

    expanded = VARIABLE.sub(variable, value)
    if expanded == "~" or expanded.startswith("~/"):
        expanded = env.get("HOME", str(Path.home())) + expanded[1:]
    path = Path(expanded)
    return str(path if path.is_absolute() else base / path)


def contained(path: Path, base: Path) -> bool:
    """Is ``path`` really inside ``base``: no symlink below ``base`` on the way to it?

    ``base`` (a configured root's fixed prefix) is trusted; a worker-writable
    symlink further down could otherwise steer a deletion somewhere else.
    """
    if not path.is_relative_to(base):
        return False
    current = base
    for part in path.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            return False
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(base))


def _matches(patterns: Iterable[str]) -> list[Path]:
    """Directories the patterns name, each reached without a symlink below its fixed prefix."""
    found: list[Path] = []
    for pattern in patterns:
        path = Path(pattern)
        if any(char in pattern for char in "*?["):
            anchor = Path(path.anchor or ".")
            relative = str(path.relative_to(anchor)) if path.is_absolute() else pattern
            matched = sorted(anchor.glob(relative))
        else:
            matched = [path]
        base = Path(_static_prefix(pattern))
        found.extend(match for match in matched if match == base or contained(match, base))
    return [path for path in dict.fromkeys(found) if path.is_dir() and not path.is_symlink()]


def mount_points(source: Path = MOUNTINFO) -> tuple[Path, ...] | None:
    """Every mount point on this host (bind mounts included); None when unknown."""
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    found: list[Path] = []
    for line in lines:
        fields = line.split()
        if len(fields) > 4:
            found.append(Path(ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), fields[4])))
    return tuple(found)


def mounted_inside(path: Path, mounts: tuple[Path, ...] | None) -> str:
    """Why deleting ``path`` could reach another filesystem; empty when it cannot."""
    if mounts is None:
        return "cannot tell what is mounted inside it"
    resolved = Path(os.path.realpath(path))
    inside = [point for point in mounts if point.is_relative_to(resolved)]
    return f"a filesystem is mounted at {inside[0]}" if inside else ""


# Measuring ----------------------------------------------------------------------------


def mount_point(path: Path) -> Path:
    current = path.absolute()
    while not current.exists():
        current = current.parent
    while not os.path.ismount(current):
        current = current.parent
    return current


def volumes(settings: DiskSettings) -> tuple[Volume, ...]:
    """Usage of each configured volume, or of "/" and every configured root's volume."""
    if settings.volumes:
        wanted = [Path(path) for path in settings.volumes]
    else:
        roots = [
            *settings.worktree_roots,
            *settings.target_roots,
            *settings.build_dirs,
            *settings.caches,
        ]
        wanted = [Path("/")] + [Path(_static_prefix(root)) for root in roots]
    found: dict[Path, Volume] = {}
    for path in wanted:
        point = mount_point(path)
        if point in found:
            continue
        try:
            usage = shutil.disk_usage(point)
        except OSError:
            continue
        found[point] = Volume(str(point), usage.total, usage.used, usage.free)
    return tuple(found.values())


def _static_prefix(pattern: str) -> str:
    parts: list[str] = []
    for part in Path(pattern).parts:
        if any(char in part for char in "*?["):
            break
        parts.append(part)
    return str(Path(*parts)) if parts else "/"


def walk(path: Path) -> Iterator[os.stat_result]:
    """Every entry under directory ``path``, not following symlinks or leaving its device."""
    try:
        device = path.lstat().st_dev
    except OSError:
        return
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    yield info
                    if stat.S_ISDIR(info.st_mode) and info.st_dev == device:
                        stack.append(Path(entry.path))
        except OSError:
            continue


def tree_size(path: Path) -> int:
    """Bytes deleting ``path`` would free: allocated blocks of files linked only inside it."""
    try:
        top = path.lstat()
    except OSError:
        return 0
    if not stat.S_ISDIR(top.st_mode):
        return _blocks(top) if top.st_nlink <= 1 else 0
    inodes: dict[tuple[int, int], list[int]] = {}  # (dev, ino) -> [blocks, nlink, seen]
    total = _blocks(top)
    for info in walk(path):
        if stat.S_ISDIR(info.st_mode) or info.st_nlink <= 1:
            total += _blocks(info)
        else:
            record = inodes.setdefault(
                (info.st_dev, info.st_ino), [_blocks(info), info.st_nlink, 0]
            )
            record[2] += 1
    return total + sum(blocks for blocks, links, seen in inodes.values() if seen >= links)


def _blocks(info: os.stat_result) -> int:
    return int(getattr(info, "st_blocks", 0)) * 512 or info.st_size


def last_activity(paths: Iterable[Path]) -> float:
    """The newest modification time anywhere in ``paths``.

    The whole tree counts: a file rewritten deep inside ``target/debug/incremental``
    leaves its ancestors' mtimes old.
    """
    newest = 0.0
    for path in paths:
        try:
            info = path.lstat()
        except OSError:
            continue
        newest = max(newest, info.st_mtime)
        if stat.S_ISDIR(info.st_mode):
            newest = max([newest, *(entry.st_mtime for entry in walk(path))])
    return newest


# Judging ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    safe: bool
    code: str
    reason: str
    repository: str = ""


def git(path: Path, *args: str) -> str | None:
    """Run git in ``path``; None when git fails."""
    try:
        done = subprocess.run(
            # No optional locks: reading status must not refresh the index, or the
            # scan itself would make every worktree look recently used.
            ["git", "--no-optional-locks", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
            env=git_environment(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_environment(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without the variables that would point git at another repository."""
    return {key: value for key, value in env.items() if key not in GIT_LOCATION_VARIABLES}


def uses(path: Path, in_use: Sequence[Path]) -> bool:
    """Does a running process hold ``path`` or anything inside it?"""
    resolved = path.resolve()
    return any(used == resolved or resolved in used.parents for used in in_use)


@dataclass(frozen=True)
class Judge:
    """Everything a verdict depends on, captured once per scan."""

    tags: frozenset[str]
    in_use: tuple[Path, ...]
    now: float
    idle_hours: float

    def _recent(self, paths: Iterable[Path]) -> bool:
        return self.now - last_activity(paths) < self.idle_hours * 3600

    def _idle_text(self) -> str:
        return f"changed in the last {self.idle_hours:g}h"

    def worktree(self, path: Path) -> Verdict:
        if path.name in self.tags:
            return Verdict(False, "live-worker", f"worker {path.name} is still running")
        if uses(path, self.in_use):
            return Verdict(False, "in-use", "a running process uses it")
        if not (path / ".git").exists():
            return Verdict(False, "not-git", "not a git checkout; left alone")
        found = git(path, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
        lines = (found or "").split()
        if len(lines) != 2:
            return Verdict(False, "git-error", "git cannot read this checkout")
        git_dir, common = Path(lines[0]), lines[1]
        if (git_dir / "locked").exists():
            return Verdict(False, "locked", "the worktree is locked", common)
        # Before the dirty check: a worker mid-change is dirty *and* recent, and a
        # recent checkout keeps its build outputs too.
        if self._recent([path, git_dir, git_dir / "logs"]):
            return Verdict(False, "recent", self._idle_text(), common)
        if any((git_dir / name).exists() for name in IN_PROGRESS):
            return Verdict(False, "in-progress", "a merge or rebase is in progress", common)
        status = git(path, "status", "--porcelain", "--untracked-files=all")
        if status is None:
            return Verdict(False, "git-error", "git status failed", common)
        if status.strip():
            count = len(status.splitlines())
            return Verdict(False, "dirty", f"{count} uncommitted or untracked change(s)", common)
        if git(path, "rev-parse", "--verify", "--quiet", "HEAD") is None:
            return Verdict(False, "no-commits", "no commit to compare with a remote", common)
        remote = (
            git(
                path,
                "for-each-ref",
                "--contains=HEAD",
                "--count=1",
                "--format=%(refname)",
                "refs/remotes",
            )
            or ""
        ).strip()
        if not remote.startswith("refs/remotes/"):
            return Verdict(False, "unpushed", "its commits are not on any remote branch", common)
        return Verdict(
            True,
            "safe",
            f"clean and on {remote.removeprefix('refs/remotes/')}; no live owner",
            common,
        )

    def target(self, path: Path) -> Verdict:
        if path.name in self.tags:
            return Verdict(False, "live-worker", f"worker {path.name} is still running")
        return self.output(path, f"no live worker named {path.name}")

    def output(self, path: Path, why: str = "regenerable build output") -> Verdict:
        owner = next((part for part in path.parts if part in self.tags), "")
        if owner:
            return Verdict(False, "live-worker", f"worker {owner} is still running")
        if uses(path, self.in_use):
            return Verdict(False, "in-use", "a running process uses it")
        if self._recent([path]):
            return Verdict(False, "recent", self._idle_text())
        return Verdict(True, "safe", why)

    def cache(self, path: Path) -> Verdict:
        if uses(path, self.in_use):
            return Verdict(False, "in-use", "a running process uses it")
        return Verdict(True, "safe", "regenerable cache; no process is using it")


def _ignored_outputs(path: Path, names: Sequence[str]) -> list[Path]:
    """Git-ignored build directories (e.g. node_modules) at most two levels down."""
    found: list[Path] = []
    for name in names:
        for pattern in (name, f"*/{name}"):
            found.extend(
                candidate
                for candidate in sorted(path.glob(pattern))
                if candidate.is_dir() and not candidate.is_symlink() and contained(candidate, path)
            )
    if not found:
        return []
    relative = [str(candidate.relative_to(path)) for candidate in found]
    ignored = git(path, "check-ignore", "--", *relative) or ""
    listed = set(ignored.splitlines())
    return [candidate for candidate, name in zip(found, relative, strict=True) if name in listed]


# Scanning -----------------------------------------------------------------------------


@dataclass
class Scanner:
    settings: DiskSettings
    processes: Processes
    keep: tuple[str, ...] = ()
    clock: Callable[[], float] = time.time
    sizer: Callable[[Path], int] = tree_size
    mounts: Callable[[], tuple[Path, ...] | None] = mount_points

    def judge(self) -> Judge:
        tags = live_tags(self.settings.ledger, self.processes)
        return Judge(
            frozenset((*tags, *self.keep)),
            self.processes.paths_in_use(),
            self.clock(),
            self.settings.idle_hours,
        )

    def candidates(self) -> list[tuple[str, Path]]:
        """Every (category, path) under the configured roots, without judging them."""
        found: list[tuple[str, Path]] = []
        for root in _matches(self.settings.worktree_roots):
            found.extend(("worktree", child) for child in _children(root))
        for root in _matches(self.settings.target_roots):
            found.extend(("target", child) for child in _children(root))
        found.extend(("build", path) for path in _matches(self.settings.build_dirs))
        found.extend(("cache", path) for path in _matches(self.settings.caches))
        return found

    def verdict(self, judge: Judge, category: str, path: Path) -> Verdict:
        if category == "worktree":
            return judge.worktree(path)
        if category == "target":
            return judge.target(path)
        if category == "cache":
            return judge.cache(path)
        return judge.output(path)

    def scan(self) -> DiskReport:
        judge = self.judge()
        mounts = self.mounts()
        judged: list[tuple[str, Path, Verdict]] = []
        for category, path in self.candidates():
            found = _unmounted(self.verdict(judge, category, path), path, mounts)
            judged.append((category, path, found))
            if category == "worktree" and found.code in IDLE_KEPT:
                # The checkout stays, but its git-ignored build outputs regenerate.
                for output in _ignored_outputs(path, self.settings.build_names):
                    verdict = judge.output(output, f"git-ignored build output of {path.name}")
                    judged.append(("build", output, _unmounted(verdict, output, mounts)))
        # A safe item inside another safe item goes with it; listing both would
        # count its blocks twice.
        safe = [path for _, path, found in judged if found.safe]
        items = [
            self._item(category, path, found)
            for category, path, found in judged
            if not (
                found.safe and any(path != other and path.is_relative_to(other) for other in safe)
            )
        ]
        items.sort(key=lambda item: (not item.safe, -item.size, item.path))
        return DiskReport(
            volumes(self.settings),
            tuple(items),
            self.settings.warn_percent,
            tuple(sorted(judge.tags)),
        )

    def _item(self, category: str, path: Path, found: Verdict) -> Item:
        size = self.sizer(path)
        return Item(
            category, str(path), size, found.safe, found.code, found.reason, found.repository
        )

    def allowed(self, category: str, path: Path) -> bool:
        """Is ``path`` one of the things the configured roots may yield?"""
        if path.is_symlink() or os.path.ismount(path):
            return False
        holder = self._holder(path) if category == "build" else None
        if holder is not None:
            return contained(path, holder)
        return (category, path) in self.candidates()

    def _holder(self, path: Path) -> Path | None:
        """The configured worktree a build output sits inside, if any."""
        for root in _matches(self.settings.worktree_roots):
            for child in _children(root):
                if path != child and path.is_relative_to(child):
                    return child
        return None

    def remove(self, report: DiskReport) -> list[Removed]:
        """Delete the report's safe items, re-checking each against the current state."""
        judge = self.judge()
        # Judge each output's worktree before deleting anything: removing an output
        # touches its parent directory, which would make the worktree look recent.
        checkouts: dict[Path, Verdict] = {}
        for item in report.safe:
            holder = self._holder(Path(item.path)) if item.category == "build" else None
            if holder is not None and holder not in checkouts:
                checkouts[holder] = judge.worktree(holder)
        removed: list[Removed] = []
        for item in report.safe:
            # Look again before every deletion: a process may have started using
            # this item while the previous one was being deleted.
            judge = self.judge()
            path = Path(item.path)
            if not path.exists():
                removed.append(Removed(item.category, item.path, 0, False, "already gone"))
                continue
            if not self.allowed(item.category, path):
                removed.append(
                    Removed(item.category, item.path, 0, False, "outside the configured roots")
                )
                continue
            found = self.verdict(judge, item.category, path)
            holder = self._holder(path) if item.category == "build" else None
            if found.safe and holder is not None:
                checkout = checkouts[holder]
                if checkout.code not in IDLE_KEPT:
                    found = Verdict(False, checkout.code, f"its worktree: {checkout.reason}")
            found = _unmounted(found, path, self.mounts())
            if not found.safe:
                removed.append(Removed(item.category, item.path, 0, False, f"kept: {found.reason}"))
                continue
            errors: list[str] = []

            def failed(
                function: Callable[..., object],
                failed_path: str,
                error: BaseException,
                errors: list[str] = errors,
            ) -> None:
                del function
                errors.append(f"{failed_path}: {error}")

            shutil.rmtree(path, onexc=failed)
            if item.category == "worktree" and found.repository:
                git(Path(found.repository), "worktree", "prune")
            if errors:
                text = f"partly removed ({len(errors)} error(s)): {errors[0]}"
                removed.append(Removed(item.category, item.path, 0, False, text))
            else:
                removed.append(Removed(item.category, item.path, item.size, True, "removed"))
        return removed


def _unmounted(found: Verdict, path: Path, mounts: tuple[Path, ...] | None) -> Verdict:
    """Keep a safe item that has a filesystem mounted inside it (rmtree would cross it)."""
    if not found.safe:
        return found
    why = mounted_inside(path, mounts)
    return Verdict(False, "mounted", why, found.repository) if why else found


def _children(root: Path) -> list[Path]:
    try:
        return sorted(
            child for child in root.iterdir() if child.is_dir() and not child.is_symlink()
        )
    except OSError:
        return []
