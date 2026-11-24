"""Adopter identity for the factory runtime.

The runtime never assumes which repository it serves. The identity comes from
GITHUB_REPOSITORY (always set in Actions) or the adopter's own
.rotisserie/config.toml, located by ROTISSERIE_CONFIG. Comment markers the
factory writes on GitHub start with marker_prefix(), derived from the
repository name, so an adopter's existing markers stay recognizable.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MARKER_PREFIX_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class AdopterConfigError(RuntimeError):
    """The adopter identity could not be determined."""


def config_path() -> Path:
    return Path(os.environ.get("ROTISSERIE_CONFIG") or ".rotisserie/config.toml")


@cache
def load_config() -> dict[str, Any]:
    path = config_path()
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise AdopterConfigError(f"invalid adopter config {path}: {exc}") from exc


def repository() -> str:
    """Return the adopter repository as OWNER/NAME."""
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not slug:
        section = load_config().get("repository") or {}
        owner, name = section.get("owner"), section.get("name")
        if owner and name:
            slug = f"{owner}/{name}"
    if not SLUG_RE.fullmatch(slug):
        raise AdopterConfigError(
            "adopter repository unknown: set GITHUB_REPOSITORY or [repository] owner/name "
            f"in {config_path()}"
        )
    return slug


def marker_prefix(slug: str | None = None) -> str:
    """Return the comment-marker prefix.

    FACTORY_MARKER_PREFIX wins when set; otherwise the repository name,
    lowercased, with each run of other characters replaced by one hyphen.
    The result is always regex- and sed-safe. factory-visibility.cjs mirrors
    this rule.
    """
    override = "" if slug else os.environ.get("FACTORY_MARKER_PREFIX", "").strip()
    if override:
        prefix = override
    else:
        name = (slug or repository()).rsplit("/", 1)[-1]
        prefix = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not MARKER_PREFIX_RE.fullmatch(prefix):
        raise AdopterConfigError(f"invalid marker prefix {prefix!r}")
    return prefix


def reserved_issues() -> frozenset[int]:
    """Issue numbers the factory never claims and never closes ([issues] reserved)."""
    raw = (load_config().get("issues") or {}).get("reserved", [])
    if not isinstance(raw, list) or not all(isinstance(n, int) and n > 0 for n in raw):
        raise AdopterConfigError(f"[issues] reserved in {config_path()} must be issue numbers")
    return frozenset(raw)


def __getattr__(name: str) -> str:
    """Resolve ``from factory_adopter import MARKER_PREFIX`` at the importer's import time."""
    if name == "MARKER_PREFIX":
        return marker_prefix()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    import sys

    commands = {
        "repository": repository,
        "marker-prefix": marker_prefix,
        "reserved-issues": lambda: json.dumps(sorted(reserved_issues())),
    }
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(f"usage: factory_adopter.py {'|'.join(commands)}")
    print(commands[sys.argv[1]]())
