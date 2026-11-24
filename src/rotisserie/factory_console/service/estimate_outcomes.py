"""What each store outcome class and claim event means to an estimate, in one table.

An estimate counts an attempt only when a model worked on the item. A class
not listed here counts as an attempt and is named in the output: an estimate
that counts too many attempts is safer than one that hides them.
"""

from __future__ import annotations

ATTEMPT = "attempt"
CAPACITY = "capacity"
IDLE = "idle"
HUMAN = "human"

ROLES = {
    ATTEMPT: "a model worked on the item",
    CAPACITY: "no model worked on it: no capacity, quota, throttle, a refused model or a "
    "broken runner",
    IDLE: "the worker found nothing to do",
    HUMAN: "a person stepped in",
}

OUTCOME_CLASSES = {
    "success": ATTEMPT,
    "work_failure": ATTEMPT,
    "agent_timeout": ATTEMPT,
    "unknown_failure": ATTEMPT,
    "no_progress": ATTEMPT,
    "switched": ATTEMPT,
    "unknown": ATTEMPT,
    "no_change": ATTEMPT,
    "policy_blocked": ATTEMPT,
    "no_work": IDLE,
    "no_capacity": CAPACITY,
    "provider_quota": CAPACITY,
    "provider_throttle": CAPACITY,
    "provider_failure": CAPACITY,
    "provider_unavailable": CAPACITY,
    "model_unavailable": CAPACITY,
    "model_retired_410": CAPACITY,
    "model_policy_violation": CAPACITY,
    "environment_failure": CAPACITY,
    "control_plane_failure": CAPACITY,
    "startup_failure": CAPACITY,
    "snapshot_failure": CAPACITY,
    "github_unavailable": CAPACITY,
}

LABEL_CLIENT = "label"
CLAIM_EVENTS = {
    "external_reserved": "claim",
    "external_revoked": "operator_revoked",
}


def role(outcome_class: str | None) -> str:
    return OUTCOME_CLASSES.get(outcome_class or "unknown", ATTEMPT)


def known(outcome_class: str | None) -> bool:
    return (outcome_class or "unknown") in OUTCOME_CLASSES


def claim_signal(event_type: str, client: str | None) -> str:
    """The human signal a claim event gives, or "" when it is not a person."""
    kind = CLAIM_EVENTS.get(event_type, "")
    if kind == "claim":
        return "owner_claim" if client == LABEL_CLIENT else ""
    return kind


def mapping() -> dict[str, object]:
    """The table itself, published with every store estimate so readers can check it."""
    return {
        "roles": ROLES,
        "outcome_classes": OUTCOME_CLASSES,
        "unlisted": ATTEMPT,
        "claim_events": {
            "external_reserved with client label": "owner_claim",
            "external_reserved with another client": "not a person (outside agent)",
            "external_revoked": "operator_revoked",
        },
    }
