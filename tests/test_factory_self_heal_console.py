"""The console and the factory agree about which stalls clear by themselves."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from rotisserie import control_contract
from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands.recap import render
from rotisserie.factory_console.github import GitHub, Obj
from rotisserie.factory_console.service import recap, self_heal, stalls
from rotisserie.factory_console.service import recap_build as build
from rotisserie.factory_console.service.stalls import Stall
from rotisserie.factory_console.ui import Console

ROOT = Path(__file__).resolve().parents[1]
HEAD = "b" * 40
NARRATION = "I'll start by reading the AGENTS.md file.\n$ git log -3\n(no output)\ngh pr diff 44"
BOT: Obj = {"login": "github-actions[bot]", "type": "Bot"}
SINCE = datetime(2026, 11, 24, 0, 0, tzinfo=UTC)


def rules() -> Any:
    return runtime.module("factory_self_heal_rules")


def verdict_record() -> Obj:
    policy = runtime.module("factory_review_policy")
    marker = policy.review_marker(pr=44, head=HEAD, reviewer="1", producer=None, verdict="repair")
    body = f"{marker}\n\n### Factory semantic review\n\n```text\n{NARRATION}\n```"
    return {"id": 7, "user": BOT, "body": body, "html_url": "https://x/c7"}


def heal_record(kind: str = "empty-verdict", ref: str = "7") -> Obj:
    text = "Moved #44 back to review: the repair verdict named no finding."
    body = rules().comment_body(kind=kind, pr=44, head=HEAD, ref=ref, text=text, avoid="m-1")
    return {
        "id": 9,
        "user": BOT,
        "body": body,
        "html_url": "https://x/c9",
        "created_at": "2026-11-24T05:00:00Z",
    }


def test_an_empty_verdict_is_a_stall_the_factory_clears() -> None:
    found = stalls.empty_repair_verdict([verdict_record()], 44, "factory:changes-requested")
    assert found is not None and found.auto
    assert "clears this by itself" in found.hint
    assert "--action review" in found.hint


def test_a_healed_verdict_is_no_longer_a_stall() -> None:
    records = [verdict_record(), heal_record()]
    assert stalls.empty_repair_verdict(records, 44, "factory:changes-requested") is None


def test_a_heal_comment_from_anyone_else_is_ignored() -> None:
    forged: Obj = {**heal_record(), "user": {"login": "someone", "type": "User"}}
    assert stalls.empty_repair_verdict([verdict_record(), forged], 44, "factory:changes-requested")
    assert self_heal.from_comments([forged], pr=44) == []


def test_heals_are_read_from_trusted_comments() -> None:
    [found] = self_heal.from_comments([heal_record()], pr=44, title="Fix counts")
    assert found.kind == "empty-verdict"
    assert found.text == "Moved #44 back to review: the repair verdict named no finding."
    assert found.url == "https://x/c9" and found.title == "Fix counts"


def test_recent_searches_once_and_reads_each_pr_found() -> None:
    calls: list[str] = []

    class Hub:
        repo = "acme/widgets"

        def api(self, route: str) -> Any:
            calls.append(route)
            return {"items": [{"number": 44, "title": "Fix counts"}]}

        def repo_api(self, route: str) -> Any:
            calls.append(route)
            return [heal_record()]

    [found] = self_heal.recent(cast(GitHub, Hub()), since=SINCE)
    assert found.pr == 44
    assert calls[0].startswith("search/issues?q=") and "Factory%20self-heal" in calls[0]
    assert calls[1].startswith("issues/44/comments?since=2026-11-24T00:00:00Z")


def test_the_recap_lists_heals_and_leaves_self_clearing_stalls_out_of_needs_you() -> None:
    sources = build.Sources(since=SINCE, now=SINCE)
    sources.stalls = [
        Stall(44, "empty-repair-verdict", "In repair on an empty verdict.", "auto", "", True),
        Stall(45, "answered-thread", "A person's thread.", "Resolve it.", ""),
    ]
    sources.healed = self_heal.from_comments([heal_record()], pr=44)
    result = recap.assemble(sources)
    assert [row["kind"] for row in result.needs_you] == ["deadlock"]
    assert "A person's thread." in str(result.needs_you)
    assert str(result.healed[0]["text"]).startswith("Moved #44")
    data = result.to_dict()
    stream = io.StringIO()
    render(Console(stream, env={"NO_COLOR": "1"}), data)
    assert "Fixed by itself (1)" in stream.getvalue()


def test_heal_is_a_live_read_with_a_bounded_window() -> None:
    assert "heal" in control_contract.LIVE_READS
    assert control_contract.read_command("heal", {}) == ["heal", "--since", "24h"]
    with pytest.raises(control_contract.InvalidRequest):
        control_contract.read_command("heal", {"since": "forever"})


def test_the_dispatcher_sweeps_and_review_selection_avoids_the_empty_verdict_model() -> None:
    dispatch = (ROOT / ".github/workflows/fixed-model-factory-dispatch.yml").read_text()
    step = dispatch.split("- name: Clear self-healable factory stalls", 1)[1].split("- name:", 1)[0]
    assert "factory_self_heal.py sweep" in step
    assert "GH_TOKEN: ${{ github.token }}" in step
    assert "FACTORY_SYNC_GH_TOKEN: ${{ secrets.PR_REBASE_TOKEN" in step
    run = (ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    prepare = run.split("id: prepare", 1)[1].split("- name:", 1)[0]
    assert "factory_self_heal.py --repo" in prepare and "avoided-reviewers" in prepare
    overlay = (ROOT / "factory/install/overlay-paths.txt").read_text().split()
    for name in (
        "factory_self_heal.py",
        "factory_self_heal_actions.py",
        "factory_self_heal_rules.py",
    ):
        assert f".github/scripts/{name}" in overlay
