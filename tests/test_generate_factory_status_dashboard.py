"""Regression coverage for the generated factory status dashboard."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "generate_factory_status_dashboard.py"
)
SPEC = importlib.util.spec_from_file_location("generate_factory_status_dashboard", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


def sample_snapshot() -> dict[str, object]:
    """Return a complete dashboard snapshot suitable for render tests."""
    return {
        "generated_at": "2026-08-24T23:50:00Z",
        "open_prs": 66,
        "open_issues": 120,
        "completion_demand": 30,
        "production_demand": 10,
        "completion_share": 0.75,
        "completion_target": 15,
        "configured_workers": 46,
        "busy_workers": 20,
        "idle_workers": 20,
        "executable_capacity": 22,
        "executable_slot_capacity": 22,
        "healthy_slots": 18,
        "degraded_slots": 4,
        "cooling_slots": 3,
        "unavailable_slots": 2,
        "executable_candidate_count": 6,
        "healthy_candidates": 5,
        "degraded_candidates": 1,
        "cooling_candidates": 2,
        "unavailable_candidates": 1,
        "pipeline": {
            "review": 8,
            "changes_requested": 21,
            "ci": 4,
            "ready": 1,
            "unowned": 3,
            "conflict": 2,
        },
        "throughput": {
            "opened_hour": 4,
            "merged_hour": 12,
            "net_hour": -8,
            "opened_day": 30,
            "merged_day": 48,
            "net_day": -18,
        },
        "funnel": {
            "completion target": "15",
            "workers selected": "15",
            "pr claims succeeded": "14",
            "updated": "2026-08-24T23:49:00Z",
        },
        "productive_workers_week": 1,
        "merged_week": 3,
        "opencode_free_roster": {
            "checked_at": "2026-09-05T20:00:00Z",
            "freshness": "live",
            "detail": (
                "Live OmniRoute probe of free OpenCode / Zen models. "
                "opencode and opencode-zen connections can both be active. "
                "Free roster models stay eligible via catalog, pricing, and freeProviders; "
                "the opencode-zen providerOverride (blanket whole-provider=free) is avoided "
                "to prevent paid bleed."
            ),
            "connections": [
                {
                    "provider": "opencode",
                    "active": True,
                    "test_status": "active",
                    "detail": "active · test active",
                },
                {
                    "provider": "opencode-zen",
                    "active": True,
                    "test_status": "active",
                    "detail": "active · test active",
                },
            ],
            "counts": {
                "ok": 2,
                "rate_limited": 1,
                "unsupported": 1,
                "catalog_miss": 1,
                "auth_paid": 1,
                "timeout": 1,
                "error": 0,
                "unknown": 0,
            },
            "models": [
                {
                    "model_id": "oc/nemotron-3.5-lightning-free",
                    "connection": "oc",
                    "status": "ok",
                    "detail": "200, $0",
                    "http_status": 200,
                },
                {
                    "model_id": "opencode-zen/nemotron-3.5-lightning-free",
                    "connection": "opencode-zen",
                    "status": "ok",
                    "detail": "200, $0",
                    "http_status": 200,
                },
                {
                    "model_id": "oc/big-pickle",
                    "connection": "oc",
                    "status": "rate_limited",
                    "detail": "429 / cooling",
                    "http_status": 429,
                },
                {
                    "model_id": "oc/laguna-s-2.1-free",
                    "connection": "oc",
                    "status": "unsupported",
                    "detail": "401 not supported",
                    "http_status": 401,
                },
                {
                    "model_id": "oc/north-mini-code-free",
                    "connection": "oc",
                    "status": "catalog_miss",
                    "detail": "400 not available in the active live catalog",
                    "http_status": 400,
                },
                {
                    "model_id": "opencode-zen/muse-spark-1.2",
                    "connection": "opencode-zen",
                    "status": "auth_paid",
                    "detail": "402 wants API key",
                    "http_status": 402,
                },
                {
                    "model_id": "oc/nemotron-3-ultra-free",
                    "connection": "oc",
                    "status": "timeout",
                    "detail": "probe timed out",
                    "http_status": None,
                },
            ],
        },
        "workers": [
            {
                "worker": "43",
                "display_name": "Laguna S 2.1 Free",
                "runtime_source": "opencode-free",
                "runtime_model": "laguna-s-2.1-free",
                "health": "healthy",
                "activity": "working",
                "attempt_outcome": "success",
                "attempt_detail": "factory job completed successfully",
                "attempt_run": "330123",
                "merged_day": 1,
                "merged_week": 3,
                "work": [
                    {
                        "kind": "pr",
                        "number": 1950,
                        "title": "Fix dashboard",
                        "url": "https://github.com/JoshCLWren/comic-pile/pull/1950",
                        "stage": "changes-requested",
                    }
                ],
                "updated": "2026-08-24T23:49:00Z",
                "tier": "proven",
            }
        ],
    }


def test_dashboard_surfaces_operational_state_and_worker_scoreboard():
    """Render fleet health, throughput, allocator state, and worker quality together."""
    rendered = dashboard.render_dashboard(sample_snapshot())

    assert "ComicPile Factory" in rendered
    assert "Working" in rendered
    assert "75%" in rendered
    assert "Changes requested" in rendered
    assert "Net PR change" in rendered
    assert "-8" in rendered
    assert "target 15 · selected 15 · claims 14" in rendered
    assert "Fleet capacity" in rendered
    assert "Executable slots" in rendered
    assert "Executable provider/models" in rendered
    assert "Fleet scoreboard" in rendered
    assert "Laguna S 2.1 Free" in rendered
    assert "laguna-s-2.1-free" in rendered
    assert "7d merges" in rendered
    assert "PR #1950" in rendered
    assert "refreshes every 5 min" in rendered
    assert "fixed-model-factory-dispatch.yml" in rendered
    assert "OpenCode free roster" in rendered
    assert "oc/nemotron-3.5-lightning-free" in rendered
    assert "opencode-zen/muse-spark-1.2" in rendered
    assert "Last checked 2026-09-05T20:00:00Z" in rendered
    assert "rate limited" in rendered
    assert "catalog miss" in rendered
    assert "providerOverride" in rendered
    assert "zen is off" not in rendered.lower()


def test_dashboard_keeps_existing_fleet_content_when_roster_is_stale():
    """A failed or missing roster probe still leaves the factory scoreboard visible."""
    snapshot = sample_snapshot()
    snapshot["opencode_free_roster"] = {
        "checked_at": "2026-09-05T20:30:00Z",
        "freshness": "stale",
        "detail": "OpenCode free-roster probe failed; table is unknown/stale.",
        "connections": [],
        "counts": {},
        "models": [],
    }

    rendered = dashboard.render_dashboard(snapshot)

    assert "OpenCode free roster" in rendered
    assert "unknown/stale" in rendered
    assert "Last checked 2026-09-05T20:30:00Z" in rendered
    assert "Fleet scoreboard" in rendered
    assert "Laguna S 2.1 Free" in rendered
    assert "zen is off" not in rendered.lower()


def test_collect_opencode_free_roster_is_fail_soft(monkeypatch: pytest.MonkeyPatch):
    """Dashboard generation survives a broken probe import or runtime crash."""

    def boom(_name: str, _path: Path):
        raise RuntimeError("probe module exploded")

    monkeypatch.setattr(dashboard, "load_module", boom)
    roster = dashboard.collect_opencode_free_roster()

    assert roster["freshness"] == "stale"
    assert "unknown/stale" in str(roster["detail"])
    assert roster["models"] == []


def test_worker_rows_join_runtime_health_merge_credit_and_current_work():
    """Rank workers using durable operational evidence instead of an invented score."""
    comments = [
        {
            "body": "\n".join(
                [
                    "<!-- factory-attempt-outcome:v1 worker=opencode-free-model-factory-43 -->",
                    "Worker: opencode-free-model-factory-43",
                    "Model: laguna-s-2.1-free",
                    "Source: opencode-free",
                    "Outcome: success",
                    "Attempt outcome: success",
                    "Detail: factory job completed successfully",
                    "Updated: 2026-08-24T23:49:00Z",
                    "Run: 330123",
                ]
            )
        },
        {
            "body": "\n".join(
                [
                    "<!-- factory-heartbeat:v1 worker=opencode-free-model-factory-43 -->",
                    "Worker: opencode-free-model-factory-43",
                    "Model: laguna-s-2.1-free",
                    "Source: opencode-free",
                    "Outcome: running",
                    "Updated: 2026-08-24T23:50:00Z",
                ]
            )
        },
    ]
    attempts, heartbeats = dashboard.latest_worker_records(comments)
    rows = dashboard.build_worker_rows(
        [
            {
                "worker": "43",
                "source": "opencode-free",
                "model": "laguna-s-2.1-free",
                "display_name": "Laguna S 2.1 Free",
            }
        ],
        capacity_rows=[
            {
                "worker": "43",
                "provider": "opencode-free",
                "model": "laguna-s-2.1-free",
                "health": "healthy",
            }
        ],
        assignments={
            "43": [
                {
                    "kind": "pr",
                    "number": 1950,
                    "title": "Fix dashboard",
                    "url": "https://github.com/JoshCLWren/comic-pile/pull/1950",
                    "stage": "review",
                }
            ]
        },
        attempts=attempts,
        heartbeats=heartbeats,
        credits={"43": {"day": 1, "week": 3, "last_merge": "2026-08-24T22:00:00Z"}},
    )

    assert rows[0]["runtime_model"] == "laguna-s-2.1-free"
    assert rows[0]["activity"] == "working"
    assert rows[0]["attempt_outcome"] == "success"
    assert rows[0]["merged_day"] == 1
    assert rows[0]["merged_week"] == 3
    assert rows[0]["tier"] == "proven"


def test_system_verdict_flags_capacity_without_claims():
    """Call attention to executable capacity that is not holding queued work."""
    snapshot = sample_snapshot()
    snapshot["busy_workers"] = 0
    snapshot["throughput"] = {
        "opened_hour": 0,
        "merged_hour": 0,
        "net_hour": 0,
        "opened_day": 3,
        "merged_day": 2,
        "net_day": 1,
    }

    severity, title, detail = dashboard.system_verdict(snapshot)

    assert severity == "warn"
    assert title == "Attention"
    assert "no factory holds work" in detail


def test_collect_snapshot_unpacks_demand_and_authoritative_capacity(
    monkeypatch: pytest.MonkeyPatch,
):
    """Use allocator capacity while enriching it with worker telemetry and merge credit."""
    work = SimpleNamespace(list_issues=lambda: [], list_prs=lambda: [])
    policy = SimpleNamespace(comment_is_trusted=lambda _comment: True)
    completion = SimpleNamespace(
        load_controller=lambda: work,
        owned_worker_ids=lambda _items: {"39"},
        registry_comments=lambda: [],
        load_policy=lambda: policy,
    )
    demand = SimpleNamespace(
        completion=7,
        production=3,
        completion_share=0.7,
        idle_workers=1,
    )
    capacity = {
        "executable_capacity": 1,
        "executable_slot_capacity": 1,
        "slot_health_counts": {
            "healthy": 1,
            "degraded": 0,
            "cooling": 1,
            "unavailable": 0,
        },
        "executable_candidate_count": 1,
        "candidate_health_counts": {
            "healthy": 1,
            "degraded": 0,
            "cooling": 0,
            "unavailable": 0,
        },
        "candidates": [
            {
                "worker": "39",
                "provider": "opencode-free",
                "model": "big-pickle",
                "health": "healthy",
            },
            {
                "worker": "40",
                "provider": "opencode-free",
                "model": "deepseek-v4-flash-free",
                "health": "cooling",
            },
        ],
    }
    full = SimpleNamespace(
        current_demand=lambda _controller: (demand, capacity),
        completion_worker_target=lambda _demand: 1,
    )

    def fake_load_module(_name: str, path: Path):
        return full if path.name == "factory_full_completion_controller.py" else completion

    monkeypatch.setattr(dashboard, "load_module", fake_load_module)
    monkeypatch.setattr(dashboard, "github_search_total", lambda *_args: 0)
    monkeypatch.setattr(dashboard, "recently_merged_prs", lambda _controller: [])
    monkeypatch.setattr(
        dashboard,
        "collect_opencode_free_roster",
        lambda: {
            "checked_at": "2026-09-05T20:00:00Z",
            "freshness": "stale",
            "detail": "probe skipped in unit test",
            "connections": [],
            "models": [],
            "counts": {},
        },
    )
    monkeypatch.setattr(
        dashboard,
        "load_manifest_rows",
        lambda _path: [
            {
                "worker": "39",
                "source": "opencode-free",
                "model": "big-pickle",
                "minute": "5",
                "scheduler": "dispatcher",
                "display_name": "OpenCode Big Pickle",
            },
            {
                "worker": "40",
                "source": "opencode-free",
                "model": "deepseek-v4-flash-free",
                "minute": "10",
                "scheduler": "dispatcher",
                "display_name": "OpenCode DeepSeek",
            },
        ],
    )

    snapshot = dashboard.collect_snapshot()

    assert snapshot["completion_demand"] == 7
    assert snapshot["production_demand"] == 3
    assert snapshot["executable_capacity"] == 1
    assert snapshot["executable_slot_capacity"] == 1
    assert snapshot["healthy_slots"] == 1
    assert snapshot["cooling_slots"] == 1
    assert snapshot["executable_candidate_count"] == 1
    assert snapshot["healthy_candidates"] == 1
    assert snapshot["cooling_candidates"] == 0
    assert snapshot["configured_workers"] == 2
    assert snapshot["busy_workers"] == 1
    assert len(snapshot["workers"]) == 2
    assert snapshot["opencode_free_roster"]["freshness"] == "stale"
