"""An owner PR that links no issue merges on its line review (owner decision 2026-11-27)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import factory_issue_acceptance as acceptance
import pytest
from pytest import MonkeyPatch
from tests.test_factory_outside_approval import load_controller
from tests.test_factory_review_gate import (
    REVIEWED_HEAD,
    load_review_controller,
    pr_payload,
    producer_contribution,
    wire_controller,
)

SCRIPTS = Path(acceptance.__file__).parent
REPO = "Owner/repo"


def owner_pr(login: str = "owner", body: str = "Docs only.") -> dict[str, Any]:
    return {"headRefName": "docs/owner-notes", "body": body, "author": {"login": login}}


@pytest.mark.parametrize(
    ("pr", "exempt"),
    [
        (owner_pr(), True),
        (owner_pr(login="OWNER"), True),
        (owner_pr(body="Closes #12."), False),
        (owner_pr(login="app/github-actions"), False),
        ({"headRefName": "docs/x", "body": "Docs only."}, False),
    ],
)
def test_only_an_owner_pr_without_an_issue_is_exempt(pr: dict[str, Any], exempt: bool) -> None:
    assert acceptance.owner_exempt(pr, REPO) is exempt


def run_cli(pr: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GITHUB_REPOSITORY": REPO}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_issue_acceptance.py"), "no-issue-mission"],
        input=json.dumps(pr),
        text=True,
        capture_output=True,
        env=env,
        cwd=SCRIPTS,
    )


def test_the_mission_tells_the_reviewer_only_for_an_owner_pr() -> None:
    owner = run_cli(owner_pr())
    assert owner.returncode == 0 and owner.stdout == acceptance.OWNER_EXEMPT_MISSION
    assert run_cli(owner_pr(login="app/github-actions")).returncode == 2


def has_report(tmp_path: Path, mission: str) -> int:
    log = tmp_path / "review.log"
    log.write_text("READY with line findings only\n", encoding="utf-8")
    script = (
        f"source {SCRIPTS / 'factory-semantic-verdict.sh'}; "
        f'factory_has_acceptance_report "$1" "$2" "$3"'
    )
    policy = str(SCRIPTS / "factory_issue_acceptance.py")
    return subprocess.run(
        ["bash", "-c", script, "_", str(log), policy, mission], capture_output=True
    ).returncode


def test_the_worker_keeps_an_owner_approval_without_an_acceptance_report(tmp_path: Path) -> None:
    trimmed = acceptance.OWNER_EXEMPT_MISSION.strip()
    assert has_report(tmp_path, trimmed) == 0
    unavailable = "ISSUE ACCEPTANCE UNAVAILABLE: no linked issue; do not approve."
    assert has_report(tmp_path, unavailable) == 1
    assert has_report(tmp_path, trimmed + "\nextra rubric line") == 1


@pytest.mark.parametrize(("login", "decision"), [("owner", "pass"), ("app/github-actions", "deny")])
def test_the_merge_gate_passes_only_an_owner_pr_without_an_issue(
    monkeypatch: MonkeyPatch, login: str, decision: str
) -> None:
    ctrl = load_controller()
    monkeypatch.setattr(ctrl, "REPO", REPO)
    monkeypatch.setattr(ctrl, "pr_json", lambda _n: owner_pr(login=login))
    monkeypatch.setattr(ctrl, "linked_issue_for_pr", lambda *_a: None)
    assert ctrl.issue_acceptance_gate(7, REVIEWED_HEAD)["decision"] == decision


@pytest.mark.usefixtures("passthrough_line_review")
@pytest.mark.parametrize(("login", "missing"), [("owner", False), ("someone-else", True)])
def test_an_owner_approval_is_not_rejected_for_a_missing_issue(
    monkeypatch: MonkeyPatch, login: str, missing: bool
) -> None:
    module = load_review_controller()
    monkeypatch.setattr(module, "REPO", REPO)
    payload = pr_payload(worker="17", branch_worker="43")
    payload.update(owner_pr(login=login))
    wire_controller(monkeypatch, module, [payload, payload], comments=producer_contribution())
    result = module.handle_review(
        worker="17",
        pr_number=1390,
        verdict="approve",
        reviewed_head=REVIEWED_HEAD,
        review_log="/tmp/model.log",
    )
    assert (result["status"] == "acceptance-issue-missing") is missing
