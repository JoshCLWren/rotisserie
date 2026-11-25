"""Looping items in ``baste turning``, ``doctor``, ``recap`` and both control rooms."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rotisserie.factory_console.commands import turning as view
from rotisserie.factory_console.service import loops, recap_build, turning
from rotisserie.factory_console.service.self_heal import Heal
from rotisserie.factory_console.service.turning_reads import DispatcherPass, WorkerRuns
from rotisserie.factory_console.ui import Console

FIXTURE = Path(__file__).parents[1] / "factory/tests/fixtures/item_loops_2026_11_24.json"
AT_0130 = datetime(2026, 11, 25, 1, 30, tzinfo=UTC)
TITLE = "Normalize the API key at one credential boundary"
PERSIST = "rotisserie / Persist trusted worker outcome in the store"


class Tracker:
    def __init__(self, rows: dict[int, dict[str, Any]]) -> None:
        self.rows = rows

    def jobs(self, run_id: int, *, version: str = "") -> list[dict[str, Any]]:
        return [{"id": run_id, "name": PERSIST, "status": "completed"}]

    def log(self, job_id: int, *, final: bool = False) -> str:
        row = self.rows[job_id]
        line = {
            "class": row["outcome"],
            "source": "nvidia",
            "model": row["model"],
            "target": row["target"],
            "kind": row["kind"],
        }
        return f"2026-11-25T00:00:00Z [factory-outcome-class] {json.dumps(line)}\n"


def recorded() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURE.read_text())["runs"])


def runs() -> list[dict[str, Any]]:
    return [
        {
            "id": row["run"],
            "status": "completed",
            "updated_at": row["finished_at"],
            "html_url": f"https://github.com/acme/widgets/actions/runs/{row['run']}",
        }
        for row in recorded()
    ]


def pull(number: int, title: str, *labels: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "labels": [{"name": name} for name in ("factory", "factory:review", *labels)],
    }


def scan(now: datetime = AT_0130, *pulls: dict[str, Any]) -> list[dict[str, object]]:
    tracker = Tracker({row["run"]: row for row in recorded()})
    return loops.scan(
        None,  # type: ignore[arg-type]
        now=now,
        tracker=tracker,  # type: ignore[arg-type]
        runs=runs(),
        pulls=list(pulls) or [pull(5766, TITLE), pull(5674, "Cover sizes"), pull(5681, "Queue")],
    )


def test_scan_reads_the_persisted_outcome_lines_and_finds_the_loop() -> None:
    found = {row["target"]: row for row in scan()}
    assert set(found) == {"pr:5766", "pr:5674"}
    row = found["pr:5766"]
    assert row["title"] == TITLE
    assert row["text"] == (
        "7 of 8 worker runs in the last 6h changed nothing; most often work_failure (5)."
    )
    assert row["next"] == (
        "Review it yourself, merge it, or force one more try with `baste run --target pr:5766`."
    )
    assert row["url"] == "https://github.com/acme/widgets/pull/5766"


def test_a_merged_pr_is_not_looping() -> None:
    assert [row["target"] for row in scan(AT_0130, pull(5674, "Cover sizes"))] == ["pr:5674"]


def evidence(**changes: Any) -> turning.Evidence:
    recent = "2026-11-25T01:25:00Z"
    base: dict[str, Any] = {
        "now": AT_0130,
        "workers": WorkerRuns(recent, recent, 2, []),
        "passes": [DispatcherPass(eligible=4, assigned=1, at=recent)],
        "newest_pass": recent,
        "backlog": {"eligible_issues": 3, "open_prs": 2, "limit": "", "limit_text": ""},
    }
    base.update(changes)
    return turning.Evidence(**base)


def test_turning_says_slowing_for_a_looping_item() -> None:
    assert turning.assess(evidence()).verdict == turning.TURNING
    verdict = turning.assess(evidence(loops=scan()))
    assert (verdict.verdict, verdict.reason, verdict.next) == (
        "slowing",
        "item-loops",
        "baste recap",
    )
    assert verdict.text == (
        f"{TITLE} is looping and 1 more: 7 of 8 worker runs in the last 6h changed nothing; "
        "most often work_failure (5)."
    )
    assert verdict.evidence["item_loops"][0]["target"] == "pr:5766"  # type: ignore[index]


def test_turning_shows_the_loop_row(capsys: pytest.CaptureFixture[str]) -> None:
    data = turning.assess(evidence(loops=scan())).to_dict()
    view.render(Console(), data, AT_0130)
    out = capsys.readouterr().out
    assert "🔁 Looping items: 2" in out
    assert f"{TITLE} (#5766): 7 of 8 worker runs" in out
    assert "👉 Review it yourself, merge it, or force one more try" in out
    assert "review  moonshotai/kimi-k3.1-pro  work_failure" in out


def test_doctor_warns_on_a_looping_item(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turning, "gather", lambda *_a, **_k: evidence(loops=scan()))
    status, text, hint = turning.health(None, AT_0130)  # type: ignore[arg-type]
    assert (status, hint) == ("warn", "baste recap")
    assert text.startswith(f"Slowing: {TITLE} is looping")


def sources(**changes: Any) -> recap_build.Sources:
    base: dict[str, Any] = {
        "since": AT_0130,
        "now": AT_0130,
        "pulls": [pull(5766, TITLE), pull(5674, "Cover sizes"), pull(5737, "Queue covers")],
    }
    base.update(changes)
    return recap_build.Sources(**base)


def parked(number: int) -> Heal:
    return Heal(
        pr=number,
        kind="loop-parked",
        words="parked a looping item",
        text="Parked it.",
        at="2026-11-25T02:00:00Z",
        url=f"https://github.com/acme/widgets/pull/{number}#c",
        title="",
    )


def test_recap_lists_looping_and_parked_items_under_needs_you() -> None:
    rows = recap_build.needs_you(sources(loops=scan(), healed=[parked(5766), parked(5737)]))
    texts = [str(row["text"]) for row in rows if row["kind"] == "loop"]
    assert texts == [
        f"{TITLE} (#5766) is looping: 7 of 8 worker runs in the last 6h changed nothing; "
        "most often work_failure (5). The factory parked it.",
        "Cover sizes (#5674) is looping: 4 of 4 worker runs in the last 6h changed nothing; "
        "most often work_failure (3).",
        "Queue covers (#5737) is parked: it was looping.",
    ]
    assert recap_build.healed(sources(healed=[parked(5766)])) == []


def test_an_owner_override_is_not_asked_of_the_owner_again() -> None:
    rows = scan(AT_0130, pull(5766, TITLE, "factory:override"))
    assert rows[0]["owner"] is True
    assert recap_build.needs_you(sources(loops=rows)) == []


def test_both_control_rooms_render_the_loop_row() -> None:
    from test_control_parity import UI  # type: ignore[import-not-found]

    assert "🔁 Looping items: " in UI and "e.item_loops" in UI
