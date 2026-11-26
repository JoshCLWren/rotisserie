"""A shared, bounded, on-disk cache of GitHub read responses.

Every ``baste`` process (each web panel runs its own) shares one directory per
repository, so panels that read the same issues, runs or variables within a
short window make one GitHub request between them. Only stdout of allowlisted
read calls is stored; ``gh`` keeps the credentials, so no token or header ever
reaches the cache. Any other ``gh`` call counts as a write: it replaces the
generation token, which orphans every mutable entry.

A *final* entry holds data that cannot change for its key, such as the log of a
completed job. It survives generation changes and lives up to ``FINAL_TTL``.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

FINAL_TTL = 7 * 24 * 3600
MUTABLE_KEEP = 3600
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
LOCK_WAIT = 30.0
GENERATION = "generation"

CACHEABLE_VERBS = frozenset(
    {
        ("issue", "list"),
        ("issue", "view"),
        ("pr", "list"),
        ("pr", "view"),
        ("variable", "list"),
        ("label", "list"),
        ("run", "list"),
    }
)
READ_VERBS = CACHEABLE_VERBS | {
    ("pr", "checks"),
    ("pr", "diff"),
    ("run", "view"),
    ("run", "download"),
    ("secret", "list"),
    ("workflow", "list"),
    ("workflow", "view"),
    ("repo", "view"),
    ("auth", "status"),
}
FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})


def classify(args: Sequence[str]) -> str:
    """``cache`` (a storable read), ``read`` (never stored) or ``write``."""
    words = list(args)
    if words[:1] != ["api"]:
        if tuple(words[:2]) in CACHEABLE_VERBS:
            return "cache"
        return "read" if tuple(words[:2]) in READ_VERBS else "write"
    method = "GET"
    fields = False
    for index, word in enumerate(words):
        if word in ("--method", "-X") and index + 1 < len(words):
            method = words[index + 1].upper()
        if word in FIELD_FLAGS:
            fields = True
    if fields and method == "GET" and "--method" not in words and "-X" not in words:
        method = "POST"
    if method == "GET":
        return "cache"
    query = next((w for w in words if w.startswith("query=")), "")
    if "graphql" in words and query and "mutation" not in query:
        return "cache"
    return "write"


def cache_root(env: Mapping[str, str]) -> Path:
    explicit = env.get("ROTISSERIE_CACHE_DIR") or os.environ.get("ROTISSERIE_CACHE_DIR")
    if explicit:
        return Path(explicit)
    base = env.get("XDG_CACHE_HOME") or os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "rotisserie"


class ReadCache:
    """One repository's cache. ``max_age`` 0 never serves a mutable entry."""

    def __init__(
        self,
        directory: Path,
        *,
        max_age: float = 0,
        clock: Callable[[], float] = time.time,
        max_total: int = MAX_TOTAL_BYTES,
    ) -> None:
        self.directory = directory
        self.max_age = max_age
        self.clock = clock
        self.max_total = max_total
        self.oldest: float | None = None

    @classmethod
    def for_repo(cls, repo: str, env: Mapping[str, str], *, max_age: float = 0) -> ReadCache:
        folder = cache_root(env) / "gh-reads" / repo.replace("/", "__")
        return cls(folder, max_age=max_age)

    def _ready(self) -> bool:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            for folder in (self.directory, self.directory.parent, self.directory.parent.parent):
                if folder.exists() and folder.stat().st_mode & 0o077:
                    folder.chmod(0o700)
        except OSError:
            return False
        return True

    def generation(self) -> str:
        try:
            return (self.directory / GENERATION).read_text(encoding="utf-8").strip() or "0"
        except OSError:
            return "0"

    def invalidate(self) -> None:
        """Orphan every mutable entry: a write happened or is about to."""
        if not self._ready():
            return
        token = f"{time.time_ns()}-{os.getpid()}"
        self._atomic(self.directory / GENERATION, token.encode("utf-8"))

    def _path(self, key: str, suffix: str) -> Path:
        return self.directory / (hashlib.sha256(key.encode("utf-8")).hexdigest() + suffix)

    def _load(self, key: str, *, final: bool, generation: str) -> str | None:
        try:
            path = self._path(key, ".final.gz" if final else ".gz")
            raw = json.loads(gzip.decompress(path.read_bytes()))
        except OSError, ValueError, EOFError:
            return None
        if not isinstance(raw, dict) or raw.get("key") != key:
            return None
        at, body = raw.get("at"), raw.get("body")
        if not isinstance(at, (int, float)) or not isinstance(body, str):
            return None
        age = self.clock() - at
        if final and raw.get("final"):
            return body if age <= FINAL_TTL else None
        stale = self.max_age <= 0 or not 0 <= age <= self.max_age
        if raw.get("final") or raw.get("gen") != generation or stale:
            return None
        self.oldest = at if self.oldest is None else min(self.oldest, at)
        return body

    def _store(self, key: str, body: str, *, final: bool, generation: str) -> None:
        record = {"key": key, "at": self.clock(), "gen": generation, "final": final, "body": body}
        blob = gzip.compress(json.dumps(record).encode("utf-8"), compresslevel=5)
        if len(blob) > MAX_ENTRY_BYTES:
            return
        self._atomic(self._path(key, ".final.gz" if final else ".gz"), blob)
        self._prune()

    def _atomic(self, path: Path, blob: bytes) -> None:
        try:
            handle, temporary = tempfile.mkstemp(dir=self.directory, prefix=".tmp-")
            with os.fdopen(handle, "wb") as stream:
                stream.write(blob)
            os.replace(temporary, path)
        except OSError:
            with contextlib.suppress(OSError, UnboundLocalError):
                os.unlink(temporary)

    def _prune(self) -> None:
        now = self.clock()
        entries: list[tuple[float, int, Path]] = []
        try:
            for entry in os.scandir(self.directory):
                if not entry.name.endswith((".gz", ".lock")):
                    continue
                stat = entry.stat()
                limit = FINAL_TTL if entry.name.endswith(".final.gz") else MUTABLE_KEEP
                if now - stat.st_mtime > limit:
                    os.unlink(entry.path)
                elif entry.name.endswith(".gz"):
                    entries.append((stat.st_mtime, stat.st_size, Path(entry.path)))
        except OSError:
            return
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= self.max_total:
                break
            with contextlib.suppress(OSError):
                path.unlink()
                path.with_suffix(".lock").unlink(missing_ok=True)
            total -= size

    @contextlib.contextmanager
    def _lock(self, key: str) -> Iterator[None]:
        if fcntl is None:
            yield
            return
        try:
            handle = os.open(self._path(key, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            yield
            return
        deadline = time.monotonic() + LOCK_WAIT
        locked = False
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        break
                    time.sleep(0.05)
            yield
        finally:
            if locked:
                fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def read_through(self, key: str, fetch: Callable[[], str], *, final: bool = False) -> str:
        """Serve a fresh enough entry, else fetch once for every waiting process."""
        if not self._ready():
            return fetch()
        generation = self.generation()
        found = self._load(key, final=final, generation=generation)
        if found is not None:
            return found
        with self._lock(key):
            generation = self.generation()
            found = self._load(key, final=final, generation=generation)
            if found is not None:
                return found
            body = fetch()
            self._store(key, body, final=final, generation=generation)
            return body


CACHED_COMMANDS = frozenset(
    {
        "status",
        "recap",
        "heal",
        "attempts",
        "splits",
        "report",
        "failures",
        "stats",
        "settings",
        "providers",
        "models",
        "why",
        "outcomes",
        "doctor",
        "leases",
        "checks",
        "review",
        "conflicts",
        "runs",
        "follow",
        "history",
        "incident",
    }
)
WRITE_FLAGS = ("fix", "yes", "requeue", "release_expired", "reset", "change", "no_wait", "repair")


def pure_read(arguments: object) -> bool:
    """Only a command that cannot write may see cached data.

    Several reads share a name with a write (``doctor --fix``, ``providers
    disable``, ``settings NAME VALUE``), so the parsed flags decide, not the name.
    """
    command = getattr(arguments, "factory_command", None)
    if command not in CACHED_COMMANDS or getattr(arguments, "via", "direct") != "direct":
        return False
    if any(getattr(arguments, flag, False) for flag in WRITE_FLAGS):
        return False
    words = getattr(arguments, "words", None) or []
    if command == "incident" and words:
        return False
    if command in ("providers", "models") and words[:1] in (["enable"], ["disable"]):
        return False
    return command != "follow" or bool(getattr(arguments, "once", False))
