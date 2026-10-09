"""Versioned operator configuration with fail-closed mutation defaults."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rotisserie.adapters.process import ProcessSpec
from rotisserie.domain import RepositoryId, WorkerId, WorkerInventory


class ConfigurationError(ValueError):
    """Raised when operator configuration is unsafe or malformed."""


@dataclass(frozen=True)
class ExecutionConfig:
    worktree: Path
    executors: tuple[ProcessSpec, ...]
    enabled: bool = False

    def __post_init__(self) -> None:
        if not self.worktree.is_absolute() or not self.executors:
            raise ConfigurationError("execution requires an absolute worktree and executors")
        if len({item.name for item in self.executors}) != len(self.executors):
            raise ConfigurationError("execution executor names must be unique")


@dataclass(frozen=True)
class OperatorConfig:
    """Configuration for one repository-scoped local operator session."""

    repository: RepositoryId
    allowed_repositories: frozenset[RepositoryId]
    snapshot: Path
    state_directory: Path
    mutations_enabled: bool = False
    workers: WorkerInventory | None = None
    execution: ExecutionConfig | None = None

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
    workers = _workers(raw.get("workers"))
    return OperatorConfig(
        repository,
        allowed,
        snapshot,
        state_directory,
        mutations_enabled,
        workers,
        _execution(raw.get("execution"), path.parent),
    )


def _execution(value: object, base: Path) -> ExecutionConfig | None:
    if value is None:
        return None
    table = _expect_mapping(value, "execution")
    if set(table) - {"worktree", "enabled", "executors"}:
        raise ConfigurationError("unknown execution configuration field")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigurationError("execution.enabled must be a boolean")
    worktree_raw = table.get("worktree")
    if not isinstance(worktree_raw, str) or not worktree_raw.strip():
        raise ConfigurationError("execution.worktree must be a path")
    worktree = (base / worktree_raw).resolve()
    entries = table.get("executors")
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("execution.executors must be a non-empty array")
    specs: list[ProcessSpec] = []
    for entry in entries:
        item = _expect_mapping(entry, "execution.executors")
        if set(item) != {"name", "argv"}:
            raise ConfigurationError("executor requires only name and argv")
        name, argv = item["name"], item["argv"]
        if not isinstance(name, str) or not isinstance(argv, list):
            raise ConfigurationError("executor requires string name and argv array")
        if not all(isinstance(argument, str) for argument in argv):
            raise ConfigurationError("executor argv requires strings")
        try:
            specs.append(ProcessSpec(name, tuple(argv)))
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
    return ExecutionConfig(worktree, tuple(specs), enabled)


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


def _workers(raw: object) -> WorkerInventory | None:
    if raw is None:
        return None
    table = _expect_mapping(raw, "workers")
    expected = _worker_set(table, "expected", required=True)
    retired = _worker_set(table, "retired", required=False)
    scheduled = _worker_set(table, "scheduled", required=False)
    try:
        return WorkerInventory(expected, retired, scheduled)
    except ValueError as exc:
        raise ConfigurationError(f"invalid workers inventory: {exc}") from exc


def _worker_set(raw: Mapping[str, Any], field: str, *, required: bool) -> frozenset[WorkerId]:
    value = raw.get(field)
    if value is None:
        if required:
            raise ConfigurationError(f"workers.{field} must be a non-empty array")
        return frozenset()
    if not isinstance(value, list):
        raise ConfigurationError(f"workers.{field} must be an array")
    if required and not value:
        raise ConfigurationError(f"workers.{field} must be a non-empty array")
    workers = [_worker_id(item, f"workers.{field}") for item in value]
    if len(set(workers)) != len(workers):
        raise ConfigurationError(f"workers.{field} contains duplicate worker identities")
    return frozenset(workers)


def _worker_id(value: object, field: str) -> WorkerId:
    if isinstance(value, str):
        namespace, separator, identity = value.partition(":")
        if not separator:
            raise ConfigurationError(f"{field} string entries must use namespace:value form")
        parts: tuple[object, object] = (namespace, identity)
    elif isinstance(value, Mapping):
        parts = (value.get("namespace"), value.get("value"))
    else:
        raise ConfigurationError(f"{field} entries must be strings or tables")
    if not all(isinstance(part, str) for part in parts):
        raise ConfigurationError(f"{field} requires string worker namespace and value")
    if any(":" in part for part in parts):  # type: ignore[operator]
        raise ConfigurationError(f"{field} namespace and value must not contain ':'")
    try:
        return WorkerId(*parts)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ConfigurationError(f"invalid {field}: {exc}") from exc


def _path(raw: Mapping[str, Any], field: str, base: Path) -> Path:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"local.{field} must be a non-empty path")
    candidate = Path(value)
    return (candidate if candidate.is_absolute() else base / candidate).resolve()
