"""Which control-room control serves each ``baste`` command.

``tests/test_control_parity.py`` reads the CLI parser and fails unless every
subcommand is listed here once: mapped to reads and writes of the shared
contract (served by both the local web UI and the hosted control room), or
listed in ``CLI_ONLY`` with the reason it has no browser control.
"""

from __future__ import annotations

CONTROLS: dict[str, tuple[str, ...]] = {
    "run": ("write:run",),
    "watch": ("read:watch",),
    "status": ("read:status",),
    "cancel": ("write:cancel",),
    "report": ("read:report",),
    "recap": ("read:recap",),
    "turning": ("read:turning",),
    "heal": ("read:heal",),
    "stats": ("read:stats",),
    "settings": ("read:settings", "write:settings"),
    "prioritize": ("write:prioritize",),
    "why": ("read:why",),
    "ready": ("read:ready",),
    "claim": ("write:claim",),
    "finish": ("write:finish",),
    "runs": ("read:runs",),
    "history": ("read:history",),
    "estimate": ("read:estimate", "write:estimate_neon"),
    "follow": ("read:follow",),
    "unstick": ("read:unstick", "write:unstick"),
    "outcomes": ("read:outcomes",),
    "close": ("write:close",),
    "doctor": ("read:doctor", "write:doctor_fix"),
    "limits": ("read:limits",),
    "failures": ("read:failures",),
    "backoffs": ("read:backoffs", "write:backoffs_reset"),
    "db-usage": ("read:db-usage",),
    "crashes": ("read:crashes", "read:crash-trace"),
    "schema": ("read:schema", "write:schema_apply"),
    "store": ("read:store", "write:store_selftest", "write:store_export", "write:store_migrate"),
    "labels": ("write:labels",),
    "conflicts": ("read:conflicts",),
    "sync": ("write:sync",),
    "leases": ("read:leases", "write:release_expired"),
    "checks": ("read:checks",),
    "review": ("read:review",),
    "allow-self-review": ("write:allow_self_review",),
    "providers": ("read:providers", "write:provider_switch"),
    "models": ("read:models", "write:model_switch"),
    "incident": ("read:incident", "write:incident_start", "write:incident_stop"),
    "assign": ("write:assign",),
    "assigned": ("read:assigned",),
    "health": ("write:health_reset",),
    "outcome": ("read:outcome",),
}

_AGENT = (
    "outside-agent claim protocol: the agent's own identity must hold the fenced "
    "claim, so an operator in a browser uses claim and finish instead"
)
_PAUSE = (
    "owner decision: pausing turns factory workflows off and writes a repository "
    "variable, which needs the owner's own gh login with the workflow scope; the "
    "operator workflow's Actions token can do neither"
)
CLI_ONLY: dict[str, str] = {
    "pause": _PAUSE,
    "resume": _PAUSE,
    "label-event": "handles one GitHub label event; only trusted Actions may run it",
    "estimate-history": (
        "publishes store history inside the label claim workflow; only trusted Actions "
        "may run it, and Exact estimate (store) starts that workflow"
    ),
    "use": "saves this machine's default repository; each control room serves one repository",
    "next": _AGENT,
    "wait-for-work": _AGENT,
    "reserve-next": _AGENT,
    "renew": _AGENT,
    "release": _AGENT,
    "handoff": _AGENT,
    "revoke": (
        "deferred: removes an outside agent's claim in the store, which the write "
        "operator workflow cannot reach yet"
    ),
    "groom": (
        "deferred: a two-step backlog edit whose apply step takes a reviewed plan file, "
        "which a single browser control cannot hold"
    ),
}
