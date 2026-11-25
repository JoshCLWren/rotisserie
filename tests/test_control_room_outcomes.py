"""Issues #205 and #207: model refusals, the scheduler decision and the lease ledger."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_factory_fleet_switch_console import (  # type: ignore[import-not-found]
    Variables,
    run,
)

from rotisserie import control_contract as contract
from rotisserie import hosted_operator, web
from rotisserie.factory_console.github import GitHub

ROOT = Path(__file__).resolve().parents[1]
UI = (ROOT / "src" / "rotisserie" / "web_ui.js").read_text(encoding="utf-8")


def published(exclusions: object) -> dict[str, str]:
    payload = {
        "generated_at": "2026-11-23T21:00:00Z",
        "models": [
            {"source": "nvidia", "model": "meta/cold", "windows": {"7d": {"runs": 3}}},
            {"source": "nvidia", "model": "meta/warm", "windows": {"7d": {"runs": 3}}},
        ],
        "providers": [],
        "model_exclusions": exclusions,
    }
    return {"content": base64.b64encode(json.dumps(payload).encode()).decode()}


def listing(monkeypatch: pytest.MonkeyPatch, exclusions: object) -> dict[str, Any]:
    found = Variables(stats=published(exclusions))
    monkeypatch.setattr(GitHub, "_run", lambda self, args, **kw: Variables._run(found, args))
    data = run(found, ["models", "nvidia"], monkeypatch)
    models: list[dict[str, Any]] = data["models"]
    return {"exclusions": data["exclusions"], **{row["model"]: row for row in models}}


def test_models_mark_what_the_worker_excludes(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = {"generated_at": "2026-11-23T21:50:00Z", "sources": {"nvidia": ["meta/cold"]}}
    rows = listing(monkeypatch, fresh)
    assert rows["exclusions"] == "published"
    assert "cooldown or backoff" in rows["nvidia/meta/cold"]["excluded"]
    assert rows["nvidia/meta/warm"]["excluded"] is None


def test_stale_or_missing_exclusions_label_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = {"generated_at": "2026-11-20T00:00:00Z", "sources": {"nvidia": ["meta/cold"]}}
    for block in (stale, None):
        rows = listing(monkeypatch, block)
        assert rows["exclusions"] is None
        assert rows["nvidia/meta/cold"]["excluded"] is None


def test_the_scheduler_decision_is_a_validated_live_read() -> None:
    assert contract.read_command("watch", {"run": "123"}) == ["watch", "123", "--decision"]
    assert "watch" in contract.LIVE_READS
    for bad in ({}, {"run": "0"}, {"run": "1; rm"}, {"run": "1", "follow": "1"}):
        with pytest.raises(contract.InvalidRequest):
            contract.read_command("watch", bad)


def test_the_lease_ledger_reads_the_store_through_a_trusted_workflow() -> None:
    assert contract.read_command("leases", {}) == ["leases", "--via", "github"]
    assert contract.read_command("leases", {}, trusted=True) == ["leases"]
    calls: list[list[str]] = []
    raw = json.dumps({"operation": "read", "reads": [{"name": "leases"}]})

    def runner(_: str, args: list[str]) -> dict[str, Any]:
        calls.append(args)
        return {}

    hosted_operator.execute("read", raw, "acme/widgets", runner)
    assert calls == [["leases"]]


def test_a_local_scheduler_decision_gets_time_to_land(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    def fake_run(command: list[str], **options: Any) -> Any:
        seen.append(options["timeout"])
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    web.invoke("acme/widgets", ["watch", "123", "--decision"])
    assert seen == [900]


def helpers(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node not installed")
    wanted = ("const refusal=", "function refusedModel(", "function decisionText(")
    lines = [line for line in UI.splitlines() if line.startswith(wanted)]
    assert len(lines) == len(wanted)
    source = "\n".join(lines) + "\nconst state={};\n" + script
    done = subprocess.run(
        [node, "-e", source], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_both_control_rooms_refuse_a_model_the_worker_would_refuse() -> None:
    found = helpers(
        'state.picker={provider:"nvidia",models:['
        '{model:"meta/cold",offered:true,excluded:"the worker excludes it right now"},'
        '{model:"meta/off",offered:true,disabled:true,disabled_reason:"flaky"},'
        '{model:"meta/gone",offered:false},{model:"meta/warm",offered:true}]};'
        'const ask=m=>refusedModel({provider:"nvidia",model:m});'
        'console.log(JSON.stringify(["meta/cold","meta/off","meta/gone","meta/warm","auto"]'
        ".map(ask)))"
    )
    cold, off, gone, warm, auto = found
    assert cold.startswith("Model meta/cold is excluded by the worker right now")
    assert "turned off (flaky)" in off
    assert "not offered by the provider" in gone
    assert warm == "" and auto == ""


def test_the_scheduler_decision_is_shown_in_words() -> None:
    found = helpers(
        "console.log(JSON.stringify(["
        'decisionText({assignment:{kind:"issue",number:7,worker:"3",provider:"nvidia",'
        'model:"meta/warm"}}),'
        'decisionText({scheduler:{status:"deferred",reason:"review backlog is full"}}),'
        'decisionText({scheduler:{status:"busy",busy_number:9}}),'
        'decisionText({error:"timed out"})]))'
    )
    assigned, deferred, busy, unknown = found
    assert assigned == "Scheduler decision: issue #7 assigned to worker 3 (nvidia, meta/warm)."
    assert deferred == "Scheduler decision: no work assigned. review backlog is full."
    assert busy == "Scheduler decision: #9 is already being worked on."
    assert unknown == "Scheduler decision: unknown. timed out."
