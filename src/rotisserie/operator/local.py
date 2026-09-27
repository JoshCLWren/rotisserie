"""Durable, repository-scoped local coordination adapter."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from rotisserie.application import EffectCommand, EffectConflict, EffectKind, GraphView
from rotisserie.domain import GraphSnapshot, RepositoryId, WorkState


class LocalStateError(RuntimeError):
    """Raised when local durable state is missing, corrupt, or out of scope."""


class LocalCoordinationPort:
    """Apply application effects to an atomic, schema-versioned JSON state file."""

    def __init__(self, source: Path, state_directory: Path, repository: RepositoryId) -> None:
        self._source = source
        self._directory = state_directory
        self._repository = repository
        self._state = state_directory / "graph-state.json"

    def initialize(self) -> None:
        if self._state.exists():
            self.read()
            return
        try:
            raw = json.loads(self._source.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise LocalStateError("snapshot root must be an object")
            snapshot = GraphSnapshot.from_dict(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise LocalStateError(f"cannot load source snapshot: {exc}") from exc
        self._validate_scope(snapshot)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._write(snapshot, version=1, applied=())

    def read(self) -> GraphView:
        raw = self._read_state()
        try:
            snapshot_raw = raw["snapshot"]
            version = raw["version"]
            if not isinstance(snapshot_raw, dict) or not isinstance(version, int):
                raise TypeError("invalid state fields")
            snapshot = GraphSnapshot.from_dict(snapshot_raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError(f"invalid local state: {exc}") from exc
        self._validate_scope(snapshot)
        return GraphView(snapshot, str(version))

    def apply(self, command: EffectCommand) -> None:
        raw = self._read_state()
        view = self.read()
        applied_raw = raw.get("applied", [])
        if not isinstance(applied_raw, list) or not all(
            isinstance(value, str) for value in applied_raw
        ):
            raise LocalStateError("invalid applied operation keys")
        applied = tuple(applied_raw)
        if command.idempotency_key in applied:
            return
        if command.work.repository != self._repository:
            raise EffectConflict("work is outside the configured repository")
        if command.expected_version != view.version:
            raise EffectConflict("local graph changed")
        if command.change is not None:
            change = next(
                (item for item in view.snapshot.changes if item.id == command.change), None
            )
            if change is None or change.head != command.expected_revision:
                raise EffectConflict("change head changed")

        snapshot = view.snapshot
        if command.kind is EffectKind.CLAIM:
            assert command.lease is not None
            snapshot = replace(snapshot, leases=(*snapshot.leases, command.lease))
        elif command.kind is EffectKind.RELEASE:
            snapshot = replace(
                snapshot,
                leases=tuple(item for item in snapshot.leases if item.id != command.lease_id),
            )
        elif command.kind is EffectKind.COMPLETE:
            snapshot = replace(
                snapshot,
                works=tuple(
                    replace(item, state=WorkState.COMPLETED) if item.id == command.work else item
                    for item in snapshot.works
                ),
            )
        self._write(snapshot, int(view.version) + 1, (*applied, command.idempotency_key))

    @property
    def state_path(self) -> Path:
        return self._state

    def _read_state(self) -> dict[str, Any]:
        if not self._state.exists():
            self.initialize()
        try:
            raw = json.loads(self._state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalStateError(f"cannot read local state: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise LocalStateError("unsupported local state schema")
        return raw

    def _write(self, snapshot: GraphSnapshot, version: int, applied: tuple[str, ...]) -> None:
        payload = {
            "schema_version": 1,
            "version": version,
            "snapshot": snapshot.to_dict(),
            "applied": list(applied),
        }
        temporary = self._state.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self._state)

    def _validate_scope(self, snapshot: GraphSnapshot) -> None:
        repositories = {
            *(item.id.repository for item in snapshot.works),
            *(item.id.repository for item in snapshot.changes),
            *(item.id.repository for item in snapshot.revisions),
        }
        if repositories - {self._repository}:
            raise LocalStateError("snapshot contains a repository outside configured scope")
