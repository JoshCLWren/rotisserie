"""A factory state label event never starts real work in the label-triggered workflows."""

from __future__ import annotations

import json
import re
from pathlib import Path

from rotisserie.factory_console import runtime

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def job_condition(name: str) -> str:
    text = (WORKFLOWS / name).read_text()
    found = re.search(r"\n    if: (?:>-\n)?(?P<body>.+?)\n    runs-on:", text, re.S)
    assert found, name
    return found["body"]


def ignored() -> list[str]:
    return list(runtime.module("factory_state_labels").IGNORED_BY_WORKFLOWS)


def test_the_list_covers_every_state_label_definition() -> None:
    module = runtime.module("factory_state_labels")
    assert set(module.DEFINITIONS) <= set(module.IGNORED_BY_WORKFLOWS)


def test_the_issue_pr_state_reconciler_skips_state_label_events() -> None:
    condition = job_condition("factory-issue-pr-state-reconciler.yml")
    listed = json.dumps(ignored(), separators=(",", ":"))
    assert (
        "!((github.event.action == 'labeled' || github.event.action == 'unlabeled')"
        f" && contains(fromJSON('{listed}'), github.event.label.name))"
    ) in condition


def test_the_issue_wakeup_skips_state_label_events() -> None:
    condition = job_condition("factory-issue-wakeup.yml")
    listed = json.dumps(ignored(), separators=(",", ":"))
    assert f"!contains(fromJSON('{listed}'), github.event.label.name)" in condition


def test_the_migration_lane_release_only_wakes_on_its_own_labels() -> None:
    condition = job_condition("factory-migration-lane-release.yml")
    allowed = json.loads(re.search(r"fromJSON\('(?P<list>\[.*?\])'\)", condition)["list"])  # type: ignore[index]
    assert allowed and not set(allowed) & set(ignored())
