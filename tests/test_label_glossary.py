"""``baste labels``: the glossary, one item's labels from a fake GitHub, both control rooms."""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rotisserie import control_contract as contract
from rotisserie.control_parity import CONTROLS
from rotisserie.factory_console import commands, runtime
from rotisserie.factory_console.commands import labels as labels_command
from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import label_explain, label_glossary
from rotisserie.factory_console.tracker import Clock, Result

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "factory/.github/scripts"


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set(value)
    return {str(item) for item in value}


def _runtime_labels() -> set[str]:
    found: set[str] = set()
    for module, names in (
        ("factory_state_labels", ("DEFINITIONS", "BYPASS", "OWNER_INPUTS", "IGNORED_BY_WORKFLOWS")),
        ("factory_label_policy", ("AVAILABLE", "CLAIM_LABELS", "OVERRIDE", "MERGE_CONFLICT")),
        ("factory_label_policy", ("LEGACY_LABELS",)),
        ("factory_item_loop", ("BYPASS_LABELS", "READY")),
        ("factory_incident", ("LABEL", "HELD_LABELS")),
        ("factory_work_policy_base", ("STAGE_LABELS", "BLOCKED_LABELS")),
        ("factory_issue_intake_guard", ("SPLIT_LABELS", "NEEDS_SPLIT")),
        ("factory_self_review_allowance", ("LABEL",)),
    ):
        loaded = runtime.module(module)
        for name in names:
            found |= _strings(getattr(loaded, name))
    return found


def _source_labels() -> set[str]:
    """Labels in modules the console cannot import: read them from the source."""
    found: set[str] = set()
    priority = ast.parse((SCRIPTS / "factory_work_policy_priority.py").read_text())
    for node in ast.walk(priority):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in ("OVERRIDE_LABEL", "INFRA_LABELS")
            for t in node.targets
        ):
            found |= {
                c.value
                for c in ast.walk(node.value)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            }
        if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In):
            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                found.add(node.left.value)
    lane = (SCRIPTS / "factory_migration_lane.py").read_text()
    found |= set(re.findall(r'^WAIT_LABEL = "([^"]+)"', lane, re.M))
    visibility = (SCRIPTS / "factory-visibility.cjs").read_text()
    block = visibility.split("const DEFINITIONS = {", 1)[1].split("\n};", 1)[0]
    found |= set(re.findall(r"^\s+'?(factory(?::[a-z-]+)?)'?: \[", block, re.M))
    return found


def test_every_factory_label_constant_has_a_glossary_entry() -> None:
    labels = _runtime_labels() | _source_labels()
    assert {
        "factory:parked",
        "factory:migration-wait",
        "factory:approved",
        "main-breakage",
    } <= labels
    missing = sorted(label for label in labels if label_glossary.lookup(label) is None)
    assert not missing, f"add glossary entries for {missing}"


def test_patterns_and_unknown_labels() -> None:
    assert label_glossary.lookup("factory:41") == label_glossary.BY_NAME["factory:<worker id>"]
    assert label_glossary.lookup("agent:claude-wexr") == label_glossary.BY_NAME["agent:*"]
    assert label_glossary.lookup("ralph-status:todo") == label_glossary.LEGACY
    assert label_glossary.lookup("ralph-status:pending") == label_glossary.BY_NAME["ralph-status:*"]
    assert label_glossary.lookup("agent:") is None and label_glossary.lookup("help wanted") is None


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["factory", "factory:review", "factory:parked"], "held: no worker until a new head"),
        (["factory:parked", "factory:override", "factory:review"], "review:"),
        (["factory:parked", "factory:incident-work", "factory:ci"], "checks:"),
        (["factory:review", "agent:claude-wexr"], "outside agent: agent:claude-wexr"),
        (["factory:blocked", "factory:migration-wait"], "waiting: another PR"),
        (["factory", "wontfix"], "blocked: no worker while wontfix is on"),
        (["factory:7", "factory:building"], "owned: factory:7"),
        (["factory:needs-split", "umbrella"], "waiting for a split"),
        (["factory:needs-split", "factory:claimed"], "claimed:"),
        (["factory", "factory:unowned"], None),
    ],
)
def test_next_step_follows_the_factory_rules(labels: list[str], expected: str | None) -> None:
    step = label_explain.next_step(labels)
    assert (step is None) if expected is None else (step or "").startswith(expected)


def run_cli(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], answers: dict[str, Any]
) -> tuple[int, str, list[str]]:
    calls: list[str] = []

    def github_run(self: GitHub, args: Sequence[str], **_: object) -> str:
        calls.append(args[1])
        return json.dumps(answers[args[1]])

    monkeypatch.setattr(GitHub, "_run", github_run)
    parser = argparse.ArgumentParser()
    commands.add_parser(parser.add_subparsers(dest="command"))
    stdout = io.StringIO()
    code = commands.main(
        parser.parse_args(["factory", *argv]),
        env={"FACTORY_REPO": "acme/widgets"},
        stdout=stdout,
        clock=Clock(now=lambda: datetime(2026, 11, 27, tzinfo=UTC)),
    )
    return code, stdout.getvalue(), calls


PARKED_PR = {
    "number": 5963,
    "title": "Fix the queue",
    "state": "open",
    "pull_request": {"url": "x"},
    "html_url": "https://github.com/acme/widgets/pull/5963",
    "labels": [{"name": n} for n in ("factory", "factory:review", "factory:parked", "help wanted")],
}


def test_item_json_reads_github_and_explains_each_label(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"repos/acme/widgets/issues/5963": PARKED_PR}
    code, out, calls = run_cli(monkeypatch, ["labels", "5963", "--json"], answers)
    assert code == 0 and calls == ["repos/acme/widgets/issues/5963"]
    data = json.loads(out)
    assert data["target"] == "pr:5963"
    rows = {row["label"]: row for row in data["labels"]}
    assert rows["help wanted"]["factory"] is False
    assert rows["help wanted"]["meaning"] == "not a factory label"
    assert rows["factory:parked"]["group"] == "state" and rows["factory:review"]["factory"]
    assert data["next"] == "held: no worker until a new head, factory:override or a claim"


def test_item_text_and_glossary_text(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = {**PARKED_PR, "state": "closed", "labels": []}
    answers = {"repos/acme/widgets/issues/12": {**closed, "number": 12}}
    code, out, _ = run_cli(monkeypatch, ["labels", "pr:12"], answers)
    assert code == 0 and "No labels." in out and "Next: closed:" in out
    code, out, calls = run_cli(monkeypatch, ["labels"], {})
    assert code == 0 and calls == []
    assert "factory:parked: The loop breaker stopped" in out and "Legacy labels" in out
    code, out, _ = run_cli(monkeypatch, ["labels", "--json"], {})
    data = json.loads(out)
    assert len(data["entries"]) == len(label_glossary.ENTRIES)
    assert "factory:torque" in data["legacy"]["labels"]


def test_doctor_flags_run_the_doctor_and_refuse_an_item(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []

    def doctor(context: object, arguments: argparse.Namespace) -> Any:
        seen.append(arguments)
        return Outcome(Result.COMPLETED, EXIT_OK, {})

    monkeypatch.setattr(labels_command, "cmd_label_doctor", doctor)
    for flags in (["--audit"], ["--repair"], ["--via", "github", "--no-wait"], ["--run", "4"]):
        code, _, _ = run_cli(monkeypatch, ["labels", *flags], {})
        assert code == 0
    assert len(seen) == 4
    code, _, calls = run_cli(monkeypatch, ["labels", "5963", "--repair"], {})
    assert code != 0 and calls == [] and len(seen) == 4
    code, _, calls = run_cli(monkeypatch, ["labels", "abc"], {})
    assert code != 0 and calls == []


def test_both_control_rooms_serve_the_glossary_and_the_item_form() -> None:
    assert contract.read_command("labels", {}) == ["labels"]
    assert contract.read_command("labels", {"target": "pr:4"}) == ["labels", "pr:4"]
    with pytest.raises(contract.InvalidRequest):
        contract.read_command("labels", {"target": "4; rm"})
    assert "labels" in contract.LIVE_READS and "labels" in contract.WRITE_OPERATIONS
    assert CONTROLS["labels"] == ("read:labels", "write:labels")
    ui = "".join(
        (ROOT / "src/rotisserie" / name).read_text() for name in ("web_ui.html", "web_ui.js")
    )
    assert 'get("labels")' in ui and 'data-item-read="labels"' in ui
    assert "labels:renderItemLabels" in ui


def test_the_label_claim_workflow_asks_for_the_doctor_explicitly() -> None:
    text = (ROOT / ".github/workflows/rotisserie-label-claim.yml").read_text()
    assert "*) args=(factory labels --audit) ;;" in text
