"""Which control-room control serves each ``baste`` command.

``tests/test_control_parity.py`` reads the CLI parser and fails unless every
subcommand is listed here once: mapped to reads and writes of the shared
contract (served by both the local web UI and the hosted control room), or
listed in ``CLI_ONLY`` with the reason it has no browser control.
"""

from __future__ import annotations

CONTROLS: dict[str, tuple[str, ...]] = {
    "run": ("write:run",),
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
}

_AGENT = (
    "outside-agent claim protocol: the agent's own identity must hold the fenced "
    "claim, so an operator in a browser uses claim and finish instead"
)
CLI_ONLY: dict[str, str] = {
    "label-event": "handles one GitHub label event; only trusted Actions may run it",
    "estimate-history": (
        "publishes store history inside the label claim workflow; only trusted Actions "
        "may run it, and Exact estimate (store) starts that workflow"
    ),
    "watch": (
        "follows one run in a terminal until it ends; both control rooms already "
        "show each request's run and its result"
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
