#!/usr/bin/env python3
"""Shared factory roster I/O, free-pin rules, and expected-worker lock.

The TSV at ``.github/free-model-factories.tsv`` is the live pin list.
``.github/factory-expected-workers.json`` is the generated lock the validator
compares against so retiring a pin updates both files together instead of
hand-editing ``EXPECTED_WORKERS``.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TypedDict

ROSTER_FIELDNAMES = (
    "worker",
    "source",
    "model",
    "minute",
    "scheduler",
    "display_name",
)
SCHEDULE_MINUTES = tuple(range(0, 60, 5))
OPENCODE_ALWAYS_FREE = frozenset({"big-pickle"})
# Time-boxed $0 OpenCode Zen promo catalog ids that omit a ``-free`` suffix.
OPENCODE_FREE_PROMO_IDS = frozenset({"union-alpha"})
BIG_PICKLE_MODEL = "big-pickle"
# Keep this many healthy big-pickle pins when converting surplus duplicates
# into newly discovered unique free OpenCode models. Extra big-pickle slots
# are the preferred add capacity so unused free models do not grow the
# roster without bound.
MIN_BIG_PICKLE_RESERVE = 1
OPENCODE_DISPLAY_TOKEN_OVERRIDES = {"mimo": "MiMo"}
OPENCODE_MUSE_SPARK_RE = re.compile(r"muse-spark", re.IGNORECASE)
_VERSIONISH_TOKEN_RE = re.compile(r"^v?\d+(?:\.\d+)*$", re.IGNORECASE)
CATALOG_SOURCES = frozenset({"opencode-free", "nvidia", "openrouter-free"})
PROTECTED_SOURCES = frozenset({"kilo-auto", "z-ai", "ollama-cloud"})
Z_AI_FREE_MODELS = frozenset({"glm-4.5-flash"})
OLLAMA_CLOUD_FREE_MODELS = frozenset({"nemotron-3-nano:30b", "gpt-oss:20b"})
# Time-boxed $0 OpenRouter promo catalog ids that omit the ``:free`` suffix.
OPENROUTER_FREE_PROMO_IDS = frozenset({"stealth/union-alpha"})
LOCK_SCHEMA_VERSION = 1


class RosterRow(TypedDict):
    """One fixed-model factory roster row."""

    worker: str
    source: str
    model: str
    minute: str
    scheduler: str
    display_name: str


class RosterLock(TypedDict):
    """Generated expected-worker lock kept in sync by retirement apply."""

    schema_version: int
    expected_workers: list[int]
    retired_workers: list[int]
    retired_models: list[str]


def _repo_root() -> Path:
    """Return the repository root that contains ``.github/``."""
    return Path(__file__).resolve().parents[2]


def default_roster_path() -> Path:
    """Return the factory TSV, preferring a cwd-relative checkout path."""
    cwd = Path(".github/free-model-factories.tsv")
    if cwd.is_file():
        return cwd
    return _repo_root() / ".github" / "free-model-factories.tsv"


def default_lock_path() -> Path:
    """Return the expected-worker lock path."""
    cwd = Path(".github/factory-expected-workers.json")
    if cwd.is_file() or cwd.parent.is_dir():
        return cwd
    return _repo_root() / ".github" / "factory-expected-workers.json"


def opencode_model_is_free(model: str) -> bool:
    """Return whether an OpenCode lane pin is a free-roster model id.

    Args:
        model: Bare OpenCode model id (no ``opencode/`` prefix).

    Returns:
        True for ``big-pickle``, ``*-free``, muse-spark free/contributor ids,
        or a time-boxed $0 Zen promo catalog id that omits ``-free``.
    """
    name = model.strip().lower()
    if not name or "/" in name:
        return False
    return (
        name in OPENCODE_ALWAYS_FREE
        or name in OPENCODE_FREE_PROMO_IDS
        or name.endswith("-free")
        or bool(OPENCODE_MUSE_SPARK_RE.search(name))
    )


def openrouter_model_is_free(model: str) -> bool:
    """Return whether an OpenRouter lane pin is an explicit free-tier model id.

    Args:
        model: OpenRouter model id.

    Returns:
        True for ``*:free`` ids, the ``openrouter/free`` auto-router, or a
        time-boxed $0 promo catalog id that omits the ``:free`` suffix.
    """
    name = model.strip()
    if name == "openrouter/free" or name in OPENROUTER_FREE_PROMO_IDS:
        return True
    return bool(name) and name.endswith(":free") and "/" in name


def _bare_source_model(model: str, source: str) -> str:
    """Return a lane pin without an optional ``{source}/`` prefix."""
    name = model.strip()
    prefix = f"{source}/"
    if name.startswith(prefix):
        return name[len(prefix) :]
    return name


def z_ai_model_is_free(model: str) -> bool:
    """Return whether a Z.AI lane pin is the permanently free GLM Flash id.

    Args:
        model: Bare or ``z-ai/``-prefixed Z.AI model id.

    Returns:
        True only for ``glm-4.5-flash``. Paid GLMs are rejected.
    """
    return _bare_source_model(model, "z-ai") in Z_AI_FREE_MODELS


def ollama_cloud_model_is_free(model: str) -> bool:
    """Return whether an Ollama Cloud lane pin is a documented free starter id.

    Args:
        model: Bare or ``ollama-cloud/``-prefixed Ollama Cloud model id.

    Returns:
        True for ``nemotron-3-nano:30b`` or the documented alt ``gpt-oss:20b``.
    """
    return _bare_source_model(model, "ollama-cloud") in OLLAMA_CLOUD_FREE_MODELS


def is_big_pickle(model: str) -> bool:
    """Return whether a roster pin is the reserved OpenCode big-pickle fallback."""
    return model.strip().lower() == BIG_PICKLE_MODEL


def opencode_free_display_name(model: str) -> str:
    """Return a TSV display name for an OpenCode free pin.

    Args:
        model: Bare OpenCode model id (no ``opencode/`` prefix).

    Returns:
        Human-readable ``OpenCode …`` label matching existing roster style.
    """
    parts: list[str] = []
    for token in model.replace("_", "-").split("-"):
        if not token:
            continue
        key = token.lower()
        override = OPENCODE_DISPLAY_TOKEN_OVERRIDES.get(key)
        if override is not None:
            parts.append(override)
            continue
        if _VERSIONISH_TOKEN_RE.fullmatch(token):
            if key.startswith("v"):
                parts.append("V" + token[1:])
            else:
                parts.append(token)
            continue
        parts.append(token[:1].upper() + token[1:] if token else token)
    return "OpenCode " + " ".join(parts)


def next_available_worker_ids(occupied: Iterable[int], count: int) -> list[int]:
    """Allocate unused worker ids after the highest occupied or retired id.

    Args:
        occupied: Worker ids already on the roster or in the retirement lock.
        count: How many new ids to allocate.

    Returns:
        Increasing worker ids that do not collide with ``occupied``.
    """
    used = {int(item) for item in occupied}
    nxt = (max(used) + 1) if used else 1
    allocated: list[int] = []
    while len(allocated) < count:
        if nxt not in used:
            allocated.append(nxt)
        nxt += 1
    return allocated


def surplus_big_pickle_rows(
    rows: Sequence[Mapping[str, str]],
    *,
    reserve: int = MIN_BIG_PICKLE_RESERVE,
) -> list[RosterRow]:
    """Return extra big-pickle slots that unused free models may convert.

    Lowest-numbered healthy big-pickle pins stay as the reserved fallback.
    Highest-numbered surplus slots convert first so historical low worker
    ids keep the catalog-free backup.

    Args:
        rows: Roster rows after retirements have already been dropped.
        reserve: Minimum big-pickle pins to keep when the catalog still
            lists the model.

    Returns:
        Convertible surplus rows, highest worker id first.
    """
    pickle_rows = [
        RosterRow(
            worker=str(row["worker"]),
            source=str(row["source"]),
            model=str(row["model"]),
            minute=str(row["minute"]),
            scheduler=str(row["scheduler"]),
            display_name=str(row["display_name"]),
        )
        for row in rows
        if str(row.get("source") or "") == "opencode-free"
        and is_big_pickle(str(row.get("model") or ""))
    ]
    pickle_rows.sort(key=lambda row: int(row["worker"]))
    if reserve < 0:
        reserve = 0
    convertible = pickle_rows[reserve:]
    convertible.reverse()
    return convertible


def load_roster_rows(path: Path | None = None) -> list[RosterRow]:
    """Load non-comment roster rows from the factory TSV.

    Args:
        path: Optional TSV path. Defaults to :func:`default_roster_path`.

    Returns:
        Roster rows in file order.
    """
    roster = path or default_roster_path()
    with roster.open(newline="", encoding="utf-8") as handle:
        rows = list(
            csv.DictReader(
                (
                    line
                    for line in handle
                    if line.strip() and not line.lstrip().startswith("#")
                ),
                fieldnames=list(ROSTER_FIELDNAMES),
                delimiter="\t",
            )
        )
    return [
        RosterRow(
            worker=str(row.get("worker") or "").strip(),
            source=str(row.get("source") or "").strip(),
            model=str(row.get("model") or "").strip(),
            minute=str(row.get("minute") or "").strip(),
            scheduler=str(row.get("scheduler") or "").strip(),
            display_name=str(row.get("display_name") or "").strip(),
        )
        for row in rows
        if str(row.get("worker") or "").strip()
    ]


def load_roster_comments(path: Path | None = None) -> list[str]:
    """Return leading ``#`` comment lines from the factory TSV."""
    roster = path or default_roster_path()
    comments: list[str] = []
    for line in roster.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            comments.append(line)
            continue
        if line.strip():
            break
    return comments


def write_roster_rows(
    path: Path,
    rows: Sequence[RosterRow],
    comments: Sequence[str] | None = None,
) -> None:
    """Write roster rows, preserving header comments when provided.

    Args:
        path: Destination TSV.
        rows: Rows to persist, written in worker-number order.
        comments: Optional comment lines to keep at the top of the file.
    """
    ordered = sorted(rows, key=lambda row: int(row["worker"]))
    lines = list(comments or [])
    for row in ordered:
        lines.append("\t".join(row[field] for field in ROSTER_FIELDNAMES))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def roster_worker_ids(rows: Sequence[Mapping[str, str]]) -> set[int]:
    """Return the integer worker ids present on the roster."""
    return {int(row["worker"]) for row in rows}


def schedule_counts(rows: Sequence[Mapping[str, str]]) -> Counter[int]:
    """Count roster rows per dispatcher minute.

    Unscheduled rows (empty or invalid ``minute``) are omitted so newly
    grown unused-free pins can be assigned during rebalance.
    """
    counts: Counter[int] = Counter()
    for row in rows:
        minute = _row_schedule_minute(row)
        if minute is not None:
            counts[minute] += 1
    return counts


def schedule_is_balanced(rows: Sequence[Mapping[str, str]]) -> bool:
    """Return whether every row is scheduled and load differs by at most 1.

    Unscheduled rows (empty or invalid ``minute``) make the roster unbalanced
    even when the remaining scheduled buckets already satisfy ±1. Discovery
    grow-path adds start with an empty minute; treating those as already
    balanced left ``validate-free-model-factories.py`` to crash on ``int('')``.
    """
    if any(_row_schedule_minute(row) is None for row in rows):
        return False
    counts = schedule_counts(rows)
    if set(counts) != set(SCHEDULE_MINUTES):
        return False
    return max(counts.values()) - min(counts.values()) <= 1


def _copy_roster_row(row: RosterRow, *, minute: int | None = None) -> RosterRow:
    """Return a roster row copy, optionally replacing the dispatcher minute."""
    return RosterRow(
        worker=row["worker"],
        source=row["source"],
        model=row["model"],
        minute=row["minute"] if minute is None else str(minute),
        scheduler=row["scheduler"],
        display_name=row["display_name"],
    )


def _row_schedule_minute(row: Mapping[str, str]) -> int | None:
    """Return a valid dispatcher minute, or None when the row is unscheduled."""
    raw = str(row.get("minute") or "").strip()
    if not raw:
        return None
    try:
        minute = int(raw)
    except ValueError:
        return None
    if minute not in SCHEDULE_MINUTES:
        return None
    return minute


def rebalance_schedule_minutes(rows: Sequence[RosterRow]) -> list[RosterRow]:
    """Reassign dispatcher minutes so remaining slots stay within ±1.

    Existing minutes are kept when the roster already satisfies
    :func:`schedule_is_balanced`. Otherwise each remaining worker keeps its
    current minute while that bucket still has capacity; overflow workers fill
    under-filled buckets in dispatcher order. Worker ids are not rewritten.

    Args:
        rows: Roster rows after pin removal or unused-free addition.

    Returns:
        Rows with balanced ``minute`` values.

    Raises:
        RuntimeError: If the remaining rows cannot be assigned to the planned
            bucket capacities. This is a programmer error, not a catalog miss.
    """
    if not rows:
        return []
    if schedule_is_balanced(rows):
        return [_copy_roster_row(row) for row in rows]

    ordered = sorted(rows, key=lambda row: int(row["worker"]))
    n = len(ordered)
    bucket_count = len(SCHEDULE_MINUTES)
    base = n // bucket_count
    extra = n % bucket_count
    current_counts = schedule_counts(ordered)
    ranked_minutes = sorted(
        SCHEDULE_MINUTES,
        key=lambda minute: (-current_counts[minute], minute),
    )
    capacity = dict.fromkeys(SCHEDULE_MINUTES, base)
    for minute in ranked_minutes[:extra]:
        capacity[minute] = base + 1

    kept_by_minute: dict[int, list[RosterRow]] = {
        minute: [] for minute in SCHEDULE_MINUTES
    }
    overflow: list[RosterRow] = []
    for row in ordered:
        minute = _row_schedule_minute(row)
        if minute is not None and len(kept_by_minute[minute]) < capacity[minute]:
            kept_by_minute[minute].append(_copy_roster_row(row, minute=minute))
        else:
            overflow.append(row)

    overflow_index = 0
    result: list[RosterRow] = []
    for minute in SCHEDULE_MINUTES:
        assigned = kept_by_minute[minute]
        while len(assigned) < capacity[minute]:
            if overflow_index >= len(overflow):
                break
            assigned.append(_copy_roster_row(overflow[overflow_index], minute=minute))
            overflow_index += 1
        result.extend(assigned)

    if overflow_index != len(overflow):
        raise RuntimeError("dispatcher minute rebalance left unassigned roster rows")
    if n >= bucket_count and not schedule_is_balanced(result):
        raise RuntimeError("dispatcher minute rebalance failed the ±1 invariant")
    return result


def empty_lock() -> RosterLock:
    """Return an empty lock document."""
    return RosterLock(
        schema_version=LOCK_SCHEMA_VERSION,
        expected_workers=[],
        retired_workers=[],
        retired_models=[],
    )


def load_roster_lock(path: Path | None = None) -> RosterLock:
    """Load the expected-worker lock, or an empty lock when the file is absent.

    Args:
        path: Optional lock path.

    Returns:
        Parsed lock document.
    """
    lock_path = path or default_lock_path()
    if not lock_path.is_file():
        return empty_lock()
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return empty_lock()
    expected = payload.get("expected_workers")
    retired_workers = payload.get("retired_workers")
    retired_models = payload.get("retired_models")
    return RosterLock(
        schema_version=int(payload.get("schema_version") or LOCK_SCHEMA_VERSION),
        expected_workers=[int(item) for item in expected] if isinstance(expected, list) else [],
        retired_workers=(
            [int(item) for item in retired_workers] if isinstance(retired_workers, list) else []
        ),
        retired_models=(
            [str(item) for item in retired_models] if isinstance(retired_models, list) else []
        ),
    )


def write_roster_lock(path: Path, lock: RosterLock) -> None:
    """Persist the expected-worker lock as stable JSON."""
    payload = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "expected_workers": sorted({int(item) for item in lock["expected_workers"]}),
        "retired_workers": sorted({int(item) for item in lock["retired_workers"]}),
        "retired_models": sorted({str(item) for item in lock["retired_models"]}),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def expected_workers(
    *,
    lock_path: Path | None = None,
    roster_path: Path | None = None,
) -> set[int]:
    """Return expected worker ids from the lock, falling back to the TSV.

    Args:
        lock_path: Optional lock path.
        roster_path: Optional TSV used only when the lock has no workers.

    Returns:
        Worker ids the validator must observe.
    """
    lock = load_roster_lock(lock_path)
    if lock["expected_workers"]:
        return set(lock["expected_workers"])
    return roster_worker_ids(load_roster_rows(roster_path))


def sync_roster_lock(
    rows: Sequence[RosterRow],
    *,
    lock_path: Path | None = None,
    extra_retired_workers: Iterable[int] = (),
    extra_retired_models: Iterable[str] = (),
) -> RosterLock:
    """Rewrite the lock from remaining rows plus accumulated retirements.

    Args:
        rows: Roster rows after retirement.
        lock_path: Destination lock. Defaults to :func:`default_lock_path`.
        extra_retired_workers: Worker ids removed in this apply.
        extra_retired_models: Model ids removed in this apply.

    Returns:
        The written lock document.
    """
    destination = lock_path or default_lock_path()
    previous = load_roster_lock(destination if destination.is_file() else None)
    lock = RosterLock(
        schema_version=LOCK_SCHEMA_VERSION,
        expected_workers=sorted(roster_worker_ids(rows)),
        retired_workers=sorted(
            set(previous["retired_workers"]) | {int(item) for item in extra_retired_workers}
        ),
        retired_models=sorted(
            set(previous["retired_models"])
            | {str(item).strip() for item in extra_retired_models if str(item).strip()}
        ),
    )
    write_roster_lock(destination, lock)
    return lock
