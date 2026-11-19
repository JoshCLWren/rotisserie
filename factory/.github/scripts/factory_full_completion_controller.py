#!/usr/bin/env python3
"""Allocate completion workers from current demand and idle fleet capacity."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from factory_capacity_policy import (
    FleetDemand,
    apply_omniroute_free_entry_cap,
    completion_worker_target,
)
from factory_work_policy import eligibility

TELEMETRY_MARKER = "<!-- factory-completion-funnel:v1 -->"


def load_controller():
    path = Path(__file__).resolve().with_name("factory_completion_controller.py")
    spec = importlib.util.spec_from_file_location("factory_completion_controller_full", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load factory_completion_controller.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def raw_work_demand(policy, issues, prs) -> tuple[int, int]:
    """Count independent completion and production demand before capacity policy.

    The legacy candidate builder intentionally suppresses fresh issue intake once
    completion backlog crosses a fixed threshold. That is an execution guard,
    not a measurement of demand. Feeding it into the ratio allocator would make
    production demand disappear at the old threshold and recreate the magic
    number indirectly. Measure structurally eligible work here instead.
    """
    issue_map = {int(issue["number"]): issue for issue in issues}
    cutover_open = eligibility.RUST_CUTOVER_EPIC in {
        number for number, issue in issue_map.items()
        if str(issue.get("state") or "OPEN").upper() == "OPEN"
    }
    allowed_prs = (
        [pr for pr in prs if policy.pr_cutover_allowed(
            pr, issue_map, cutover_open=True
        )]
        if cutover_open else prs
    )
    completion = sum(
        policy.pr_is_static_candidate(pr, issue_map) for pr in allowed_prs
    )

    suppressing_pr_issues = {
        linked
        for pr in prs
        if (linked := policy.linked_issue_from_pr(pr)) is not None
        and policy.pr_suppresses_issue_candidate(pr, issue_map)
    }
    production = sum(
        policy.issue_is_static_candidate(
            issue,
            suppressing_pr_issues,
            no_diff_attempts=0,
        )
        and (
            not cutover_open or eligibility.rust_cutover_work_allowed(
                policy.labels_of(issue), str(issue.get("body") or ""),
                cutover_open=True,
            )
        )
        for issue in issues
    )
    return completion, production


def current_demand(controller, *, now_epoch: int | None = None) -> tuple[FleetDemand, dict[str, object]]:
    """Measure work demand against idle, evidence-backed executable capacity."""
    work_controller = controller.load_controller()
    policy = controller.load_policy()
    issues = work_controller.list_issues()
    prs = work_controller.list_prs()
    completion, production = raw_work_demand(policy, issues, prs)
    if completion == 0:
        # This workflow only wakes completion workers. With no completion
        # demand, there is no reason to paginate the heartbeat registry,
        # compute provider health, or read in-flight entry capacity. Production
        # intake still runs through the separate serialized dispatcher.
        return (
            FleetDemand(completion=0, production=production, idle_workers=0),
            {
                "owned_workers": [],
                "executable_candidates": [],
                "capacity_scan_skipped": True,
                "executable_slot_capacity": 0,
            },
        )

    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    candidates = controller.load_manifest_candidates()
    owned = controller.owned_worker_ids([*issues, *prs])
    try:
        _view, health = controller.durable_factory_state(now_epoch=now_epoch)
    except RuntimeError as exc:
        print(
            f"[factory-completion] capacity evidence unavailable; failing closed: {exc}",
            file=sys.stderr,
        )
        health = {}
    capacity = controller.capacity_report(candidates, health, now_epoch=now_epoch)
    # Signal-only dispatch may reuse the same authoritative read-only snapshot.
    # Actual claims still refresh live labels/head in the serialized dispatcher.
    capacity["owned_workers"] = sorted(owned)
    idle = sum(
        candidate["worker"] not in owned
        and controller.worker_is_executable(
            candidate["worker"],
            health,
            now_epoch=now_epoch,
        )
        for candidate in candidates
    )
    demand = apply_omniroute_free_entry_cap(
        FleetDemand(completion=completion, production=production, idle_workers=idle),
        work_controller.in_flight_omniroute_free_entries(),
    )
    return demand, capacity


def persist_funnel_telemetry(result: dict[str, object]) -> None:
    """Publish demand, allocation, and claim results to the Actions job summary.

    The trusted Actions run (its log and job summary) is the operational
    record. Issue #1093 is retired as a registry and is never written.
    """
    selected = list(result.get("selected_workers") or [])
    # Most explicit wakeups have no executable work; skip their summary.
    if not selected and os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return
    assignments = list(result.get("assignments") or [])
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    body = "\n".join(
        [
            TELEMETRY_MARKER,
            "## Factory completion funnel",
            f"Completion demand: {result.get('completion_demand', 0)}",
            f"Production demand: {result.get('production_demand', 0)}",
            f"Idle executable workers: {result.get('idle_workers', 0)}",
            f"Executable slot capacity: {result.get('executable_slot_capacity', 0)}",
            "Slot health: "
            + json.dumps(result.get("slot_health_counts", {}), sort_keys=True),
            f"Executable provider/model candidates: "
            f"{result.get('executable_candidate_count', 0)}",
            "Candidate health: "
            + json.dumps(result.get("candidate_health_counts", {}), sort_keys=True),
            "Executable provider/models: "
            + (
                ", ".join(
                    f"{item.get('provider')}/{item.get('model')}"
                    for item in result.get("executable_provider_models", [])
                    if isinstance(item, dict)
                )
                or "none"
            ),
            "Completion capacity evidence: "
            + ("not measured; no completion demand" if result.get("capacity_scan_skipped") else "measured"),
            f"Completion share: {float(result.get('completion_share', 0.0)):.3f}",
            f"Completion target: {result.get('completion_target', 0)}",
            f"Workers selected: {len(selected)}",
            f"PR claims succeeded: {len(assignments)}",
            "Selected worker IDs: " + (", ".join(map(str, selected)) if selected else "none"),
            "Assignments: "
            + (
                ", ".join(
                    f"Factory {item.get('worker')} → PR #{item.get('number')}"
                    for item in assignments
                    if isinstance(item, dict)
                )
                if assignments
                else "none"
            ),
            f"Updated: {now}",
        ]
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(body + "\n")
    except OSError as exc:
        print(f"[factory-completion] unable to write funnel summary: {exc}", file=sys.stderr)


def signal_dispatch(controller, demand: FleetDemand, capacity: dict[str, object]) -> dict[str, object]:
    """Signal the dispatcher using the already-fetched, read-only demand snapshot.

    The serialized dispatcher, not this signal-only workflow, reconciles stale
    leases and performs authoritative live rechecks before any claim or launch.
    Duplicating reconciliation here consumed active-run, issue and PR API pages
    again for every worker completion, even when no work could be claimed.
    """
    target = completion_worker_target(demand)
    owned_snapshot = capacity.get("owned_workers")
    owned = (
        set(map(str, owned_snapshot))
        if isinstance(owned_snapshot, (list, tuple, set))
        else set()
    )

    executable_candidates = list(capacity.get("executable_candidates") or [])
    eligible = [
        str(candidate["worker"])
        for candidate in executable_candidates
        if str(candidate["worker"]) not in owned
    ]
    eligible.sort(key=int)

    # Determine mode: explicit-worker for one-for-one, roster for broad bootstrap
    mode = "explicit-worker" if 0 < target <= 1 else "roster"

    return {
        "signal": mode,
        "selected_workers": eligible[:target] if target > 0 else [],
        "completion_demand": demand.completion,
        "production_demand": demand.production,
        "idle_workers": demand.idle_workers,
        "completion_share": demand.completion_share,
        "completion_target": target,
        "idle_executable_workers": len(eligible),
        "capacity": capacity,
        "capacity_scan_skipped": bool(capacity.get("capacity_scan_skipped", False)),
        "assignments": [],
    }


def main() -> int:
    controller = load_controller()
    demand, capacity = current_demand(controller)
    result = signal_dispatch(controller, demand, capacity)
    persist_funnel_telemetry(result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
