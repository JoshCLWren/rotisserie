#!/usr/bin/env python3
"""The migration families an adopter declares: where migrations live and how versions are named.

An adopter lists each family in ``.rotisserie/config.toml``::

    [[migrations]]
    family = "refinery"
    directory = "native/migrate/migrations/"
    version = '^V(?P<version>[0-9]+)__[A-Za-z0-9_]+\\.sql$'
    increasing = true

``version`` is matched against the file name and must name a ``version`` group of
digits. ``increasing`` (default false) also rejects a new version that is not higher
than every version on the base branch, for tools such as Refinery that refuse
migrations older than the last applied one.

With no ``[[migrations]]`` table the one family is Alembic under
``alembic/versions/``, whose revision ids are not ordered numbers, so it has no
version pattern. An invalid table is an error: the lane must not guess.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from factory_adopter import AdopterConfigError, config_path, load_config

DEFAULT = ({"family": "alembic", "directory": "alembic/versions/"},)
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DIRECTORY_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)+")


@dataclass(frozen=True)
class Family:
    name: str
    directory: str
    version: re.Pattern[str] | None
    increasing: bool = False

    def owns(self, path: str) -> bool:
        return path.startswith(self.directory)


@dataclass(frozen=True)
class Change:
    """One migration file a PR touches; ``ambiguous`` when the lane cannot tell its version."""

    family: str
    path: str
    status: str
    version: int | None
    ambiguous: bool


def _family(raw: Any, position: int) -> Family:
    where = f"[[migrations]] entry {position} in {config_path()}"
    if not isinstance(raw, Mapping):
        raise AdopterConfigError(f"{where} must be a table")
    name, directory = raw.get("family"), raw.get("directory")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise AdopterConfigError(f"{where}: family must be a lowercase name")
    if not isinstance(directory, str) or not DIRECTORY_RE.fullmatch(directory):
        raise AdopterConfigError(f"{where}: directory must be a relative path ending in /")
    increasing = raw.get("increasing", False)
    if not isinstance(increasing, bool):
        raise AdopterConfigError(f"{where}: increasing must be true or false")
    pattern = raw.get("version")
    if pattern is None:
        if increasing:
            raise AdopterConfigError(f"{where}: increasing needs a version pattern")
        return Family(name, directory, None)
    if not isinstance(pattern, str):
        raise AdopterConfigError(f"{where}: version must be a regular expression")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise AdopterConfigError(f"{where}: version is not a valid pattern: {exc}") from None
    if "version" not in compiled.groupindex:
        raise AdopterConfigError(f"{where}: version must name a (?P<version>...) group")
    return Family(name, directory, compiled, increasing)


def families(config: Mapping[str, Any] | None = None) -> tuple[Family, ...]:
    """The adopter's migration families, validated; Alembic when none are declared."""
    raw = (load_config() if config is None else config).get("migrations", DEFAULT)
    if not isinstance(raw, list | tuple) or not raw:
        raise AdopterConfigError(f"[[migrations]] in {config_path()} must list at least one family")
    found = tuple(_family(entry, n) for n, entry in enumerate(raw, start=1))
    names = [family.name for family in found]
    if len(set(names)) != len(names):
        raise AdopterConfigError(f"[[migrations]] in {config_path()} repeats a family name")
    for first in found:
        for other in found:
            if first is not other and other.owns(first.directory):
                raise AdopterConfigError(
                    f"[[migrations]] directories overlap: {first.directory} and {other.directory}"
                )
    return found


def fingerprint(declared: Iterable[Family]) -> str:
    """A short stable id of the declared families, so cached answers change with them."""
    shape = [
        [
            f.name,
            f.directory,
            f.version.pattern if f.version else "",
            *(["increasing"] * f.increasing),
        ]
        for f in declared
    ]
    return hashlib.sha256(json.dumps(shape).encode()).hexdigest()[:12]


def changes(row: Mapping[str, Any], declared: Iterable[Family]) -> list[Change]:
    """The migration files one GitHub changed-file row touches, before and after a rename."""
    status = str(row.get("status") or "")
    found: list[Change] = []
    for key in ("filename", "previous_filename"):
        path = str(row.get(key) or "")
        for family in declared:
            if not path or not family.owns(path):
                continue
            if family.version is None:
                found.append(Change(family.name, path, status, None, False))
                continue
            match = family.version.fullmatch(path[len(family.directory) :])
            digits = match.group("version") if match else ""
            version = int(digits) if digits.isdigit() else None
            ambiguous = version is None or status != "added"
            found.append(Change(family.name, path, status, version, ambiguous))
    return found
