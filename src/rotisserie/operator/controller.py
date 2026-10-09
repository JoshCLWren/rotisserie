"""Strict, credential-free acquisition envelope for the controller CLI."""

from __future__ import annotations

from typing import Any

from rotisserie.application.controller import ControllerView, Liveness
from rotisserie.application.coordination import GraphView
from rotisserie.application.process import _secret_free
from rotisserie.domain import GraphSnapshot, WorkerId


def controller_view_from_dict(value: object) -> ControllerView:
    _secret_free(value)
    fields = {
        "schema_version",
        "version",
        "snapshot",
        "observed_at",
        "active",
        "completion_backlog",
        "liveness",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid controller acquisition fields")
    raw: dict[str, Any] = value
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported controller acquisition version")
    if not isinstance(raw["version"], str) or not raw["version"].strip():
        raise ValueError("controller acquisition requires a version")
    for key in ("observed_at", "active", "completion_backlog"):
        if type(raw[key]) is not int or raw[key] < 0:
            raise ValueError("controller acquisition requires non-negative counts and time")
    if not isinstance(raw["snapshot"], dict) or not isinstance(raw["liveness"], list):
        raise ValueError("invalid controller snapshot or liveness")
    liveness: list[tuple[WorkerId, Liveness]] = []
    for item in raw["liveness"]:
        if not isinstance(item, dict) or set(item) != {"namespace", "value", "state"}:
            raise ValueError("invalid controller worker observation")
        if not all(isinstance(item[x], str) for x in item):
            raise ValueError("controller worker observation requires strings")
        liveness.append((WorkerId(item["namespace"], item["value"]), Liveness(item["state"])))
    return ControllerView(
        GraphView(GraphSnapshot.from_dict(raw["snapshot"]), raw["version"]),
        raw["observed_at"],
        raw["active"],
        raw["completion_backlog"],
        tuple(liveness),
    )
