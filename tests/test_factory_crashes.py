"""``baste crashes``, the crash Burning rule in ``turning``, doctor and both control rooms."""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_factory_console import START, FakeGitHub, fake, invoke  # type: ignore[import-not-found]
from test_factory_turning import evidence, stalled_routes  # type: ignore[import-not-found]

from rotisserie import control_contract as contract
from rotisserie.control_parity import CONTROLS
from rotisserie.factory_console.service import crashes, turning

__all__ = ["fake"]

NOW = START
ROOT = Path(__file__).resolve().parents[1]
RUN = "https://github.com/acme/widgets/actions/runs/45655094820"


def stamp(delta: timedelta) -> str:
    return str((NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ"))


def group(
    *,
    minutes: tuple[int, ...] = (39, 30, 20, 10, 2),
    workflow: str = "fixed-model-factory-dispatch.yml",
    fatal: bool = True,
) -> dict[str, Any]:
    times = [stamp(timedelta(minutes=m)) for m in sorted(minutes)]
    return {
        "fingerprint": "5c0ffee1d00d",
        "kind": "python" if fatal else "item",
        "fatal": fatal,
        "script": "factory-work-controller.py",
        "function": "replace_factory_labels",
        "line": 236,
        "exception_type": "UnboundLocalError",
        "message": "cannot access local variable 'checked_at' where it is not associated",
        "workflow": workflow,
        "workflows": [workflow],
        "count": len(times),
        "first_at": times[-1],
        "last_at": times[0],
        "last_run_url": RUN,
        "times": times,
    }


def test_a_dispatcher_crash_in_the_window_burns_even_while_workers_start() -> None:
    verdict = turning.assess(evidence(crashes=[group()]))
    assert (verdict.verdict, verdict.reason, verdict.next) == ("burning", "crash", "baste crashes")
    assert verdict.text.startswith(
        "UnboundLocalError in factory-work-controller.py:236 replace_factory_labels crashed "
        "the dispatcher 5 times in the last 60m"
    )
    assert verdict.text.isascii()
    shown = verdict.evidence["crashes"]
    assert isinstance(shown, list) and shown[0]["count"] == 5


@pytest.mark.parametrize(
    "found",
    [
        group(minutes=(90, 75)),
        group(workflow="factory-status-pages.yml"),
        group(fatal=False),
    ],
    ids=["older-than-the-window", "not-dispatch-or-drain", "isolated-item-failure"],
)
def test_other_crashes_do_not_burn(found: dict[str, Any]) -> None:
    verdict = turning.assess(evidence(crashes=[found]))
    assert (verdict.verdict, verdict.reason) == ("turning", "moving")


def test_a_merge_drain_crash_burns() -> None:
    verdict = turning.assess(evidence(crashes=[group(workflow="factory-ready-merge-drain.yml")]))
    assert verdict.reason == "crash" and "crashed the merge drain" in verdict.text


def test_the_published_groups_are_recounted_over_the_asked_window() -> None:
    found = crashes.within([group()], NOW - timedelta(minutes=15))
    assert found[0]["count"] == 2 and found[0]["first_at"].startswith("2026-11-21T22:50")
    assert crashes.within([group()], NOW - timedelta(minutes=1)) == []


def stats_with(*groups: dict[str, Any]) -> dict[str, Any]:
    block = {
        "generated_at": stamp(timedelta(minutes=3)),
        "window_hours": 24,
        "groups": list(groups),
    }
    return {
        "models": [{"source": "nvidia", "model": "m", "health": {"status": "healthy"}}],
        "db_usage": {"status": "ok", "checked_at": stamp(timedelta(minutes=10))},
        "crashes": block,
    }


def published(fake: FakeGitHub, stats: dict[str, Any]) -> None:
    content = base64.b64encode(json.dumps(stats).encode()).decode()
    fake.routes[r"contents/factory-stats\.json"] = {"content": content}


def test_turning_shows_the_crash_and_says_burning(fake: FakeGitHub) -> None:
    stalled_routes(fake)
    published(fake, stats_with(group()))
    code, out = invoke(["turning", "--json"])
    data = json.loads(out)
    assert code == 0 and (data["verdict"], data["reason"]) == ("burning", "crash")
    assert "💥" not in out
    code, out = invoke(["turning"])
    assert "🔥🔥 Burning" in out and "👉 Next: baste crashes" in out
    assert (
        "💥 Crash: UnboundLocalError in factory-work-controller.py:236 replace_factory_labels, "
        "5 times since "
    ) in out
    assert f"last run {RUN}" in out


def test_baste_crashes_lists_the_published_groups(fake: FakeGitHub) -> None:
    published(fake, stats_with(group(), group(minutes=(600,))))
    code, out = invoke(["crashes", "--since", "6h"])
    assert code == 0
    assert "Factory crashes since" in out and ": 1 groups" in out
    assert "💥 Crash: UnboundLocalError in factory-work-controller.py:236" in out
    assert "5c0ffee1d00d" in out and "baste crashes --show FINGERPRINT" in out
    code, out = invoke(["crashes", "--json", "--since", "12h"])
    data = json.loads(out)
    assert code == 0 and [g["count"] for g in data["groups"]] == [5, 1]
    assert all("traceback" not in g for g in data["groups"])


def test_show_reads_the_traceback_through_the_operator_workflow(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def via(github: object, clock: object, fingerprint: str, **_: object) -> dict[str, Any]:
        asked.append(fingerprint)
        return {**group(), "found": True, "traceback": "Traceback ...\nUnboundLocalError: x"}

    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    monkeypatch.setattr(crashes, "show_via_github", via)
    code, out = invoke(["crashes", "--show", "5c0ffee1"])
    assert code == 0 and asked == ["5c0ffee1"]
    assert "UnboundLocalError: x" in out
    code, out = invoke(["crashes", "--show", "x!"])
    assert code != 0


def test_show_reads_the_store_directly_inside_a_trusted_job(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store.db"
    schema = (ROOT / "factory/.github/factory-state.sqlite.sql").read_text()
    conn = sqlite3.connect(store)
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO factory_crash (repo, fingerprint, kind, script, function, line,"
        " exception_type, message, traceback, workflow, run_url, created_at)"
        " VALUES ('acme/widgets', '5c0ffee1d00d', 'python', 'factory-work-controller.py',"
        " 'replace_factory_labels', 236, 'UnboundLocalError', 'checked_at',"
        " 'Traceback (most recent call last):\nUnboundLocalError: checked_at',"
        " 'fixed-model-factory-dispatch.yml', ?, 1795300000)",
        (RUN,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(store))
    code, out = invoke(["crashes", "--show", "5c0ffee1", "--json"])
    data = json.loads(out)
    assert code == 0 and data["found"] and data["count"] == 1
    assert data["traceback"].endswith("UnboundLocalError: checked_at")


def test_doctor_fails_on_a_dispatcher_crash_and_warns_on_another() -> None:
    status, text, hint = crashes.health(stats_with(group()), NOW)
    assert status == "fail" and "crashed the dispatcher" in text and hint == "baste crashes"
    other = group(workflow="factory-status-pages.yml")
    assert crashes.health(stats_with(other), NOW)[0] == "warn"
    assert crashes.health(stats_with(group(minutes=(300,))), NOW)[0] == "ok"
    assert crashes.health(None, NOW) == ("ok", "no crash groups published yet", "")


def test_both_control_rooms_read_crashes_and_a_traceback() -> None:
    assert contract.read_command("crashes", {}) == ["crashes", "--since", "6h"]
    assert contract.read_command("crash-trace", {"fingerprint": "5c0ffee1d00d"}) == [
        "crashes",
        "--show",
        "5c0ffee1d00d",
    ]
    with pytest.raises(contract.InvalidRequest):
        contract.read_command("crash-trace", {"fingerprint": "; rm -rf /"})
    assert "crashes" in contract.PUBLIC_READS and "crash-trace" in contract.LIVE_READS
    assert CONTROLS["crashes"] == ("read:crashes", "read:crash-trace")
    ui = (ROOT / "src/rotisserie/web_ui.js").read_text()
    assert 'get("crashes")' in ui and 'get("crash-trace"' in ui
    hosted = (ROOT / "factory/scripts/hosted/public_reads.js").read_text()
    assert 'name === "crashes"' in hosted and "stats.crashes" in hosted
