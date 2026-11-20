"""Adopter identity for the factory runtime.

The runtime never assumes which repository it serves. The identity comes from
GITHUB_REPOSITORY (always set in Actions) or the adopter's own
.rotisserie/config.toml, located by ROTISSERIE_CONFIG.
"""

from __future__ import annotations

import os
import re
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


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
