"""Versioned operator configuration with fail-closed mutation defaults."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rotisserie.domain import RepositoryId


class ConfigurationError(ValueError):
    """Raised when operator configuration is unsafe or malformed."""


@dataclass(frozen=True)
class OperatorConfig:
    """Configuration for one repository-scoped local operator session."""

    repository: RepositoryId
    allowed_repositories: frozenset[RepositoryId]
    snapshot: Path
    state_directory: Path
    mutations_enabled: bool = False

    def __post_init__(self) -> None:
        if self.repository not in self.allowed_repositories:
            raise ConfigurationError("repository is not in repositories.allow")
        if not self.snapshot.is_absolute() or not self.state_directory.is_absolute():
            raise ConfigurationError("operator paths must be absolute after loading")


def load_config(path: Path) -> OperatorConfig:
    """Load schema version 1 TOML, resolving paths relative to its directory."""

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration: {exc}") from exc
    if raw.get("schema_version") != 1:
        raise ConfigurationError("unsupported configuration schema_version")
    repository = _repository(_mapping(raw, "repository"), "repository")
    repositories = _mapping(raw, "repositories")
    allowed_raw = repositories.get("allow")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise ConfigurationError("repositories.allow must be a non-empty array")
    allowed = frozenset(
        _repository(_expect_mapping(value, "repositories.allow"), "repositories.allow")
        for value in allowed_raw
    )
    local = _mapping(raw, "local")
    snapshot = _path(local, "snapshot", path.parent)
    state_directory = _path(local, "state_directory", path.parent)
    mutations_enabled = local.get("mutations_enabled", False)
    if not isinstance(mutations_enabled, bool):
        raise ConfigurationError("local.mutations_enabled must be a boolean")
    return OperatorConfig(repository, allowed, snapshot, state_directory, mutations_enabled)


def _mapping(raw: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    return _expect_mapping(raw.get(field), field)


def _expect_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a table")
    return value


def _repository(raw: Mapping[str, Any], field: str) -> RepositoryId:
    values = (raw.get("host"), raw.get("owner"), raw.get("name"))
    if not all(isinstance(value, str) for value in values):
        raise ConfigurationError(f"{field} requires string host, owner, and name")
    try:
        return RepositoryId(*values)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ConfigurationError(f"invalid {field}: {exc}") from exc


def _path(raw: Mapping[str, Any], field: str, base: Path) -> Path:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"local.{field} must be a non-empty path")
    candidate = Path(value)
    return (candidate if candidate.is_absolute() else base / candidate).resolve()
