"""Strict, credential-free acquisition envelope for the controller CLI."""

from __future__ import annotations

from typing import Any

from rotisserie.application.controller import ControllerView, Liveness
from rotisserie.application.coordination import GraphView
from rotisserie.application.process import _secret_free
from rotisserie.application.recovery import ActivityEvidence, RecoveryView, lease_to_dict
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


def recovery_view_from_dict(value: object) -> RecoveryView:
    """Decode a captured adapter attestation; fixtures confer no live authority."""
    _secret_free(value)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "controller",
        "activity",
        "liveness_complete",
    }:
        raise ValueError("invalid recovery acquisition fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported recovery acquisition version")
    controller = controller_view_from_dict(value["controller"])
    if not isinstance(value["activity"], list):
        raise ValueError("invalid recovery activity")
    activity = []
    for item in value["activity"]:
        if not isinstance(item, dict) or set(item) != {
            "lease",
            "observed_at",
            "latest_activity_at",
            "trusted",
        }:
            raise ValueError("invalid recovery activity evidence")
        if not isinstance(item["lease"], dict) or any(
            type(item["lease"].get(key)) is not int for key in ("acquired_at", "expires_at")
        ):
            raise ValueError("exact lease timestamps require integers")
        lease = next(
            (x for x in controller.graph.snapshot.leases if lease_to_dict(x) == item["lease"]), None
        )
        if lease is None:
            raise ValueError("activity must bind an exact captured lease")
        if type(item["observed_at"]) is not int:
            raise ValueError("activity observation time is required")
        activity.append(
            ActivityEvidence(
                lease, item["observed_at"], item["latest_activity_at"], item["trusted"]
            )
        )
    return RecoveryView(controller, tuple(activity), value["liveness_complete"])
