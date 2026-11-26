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


def test_the_pr_review_wakeup_and_label_claim_wake_only_on_their_own_label() -> None:
    review = job_condition("factory-pr-review-wakeup.yml")
    assert "github.event.action == 'labeled' && github.event.label.name == 'factory:review'" in (
        review
    )
    claim = (WORKFLOWS / "rotisserie-label-claim.yml").read_text()
    assert "github.event.label.name == 'factory:claimed')" in claim


def test_the_owner_incident_label_still_wakes_work() -> None:
    module = runtime.module("factory_state_labels")
    assert runtime.module("factory_incident").LABEL in module.OWNER_INPUTS
    assert not set(module.OWNER_INPUTS) & set(module.IGNORED_BY_WORKFLOWS)
    for name in ("factory-issue-wakeup.yml", "factory-issue-pr-state-reconciler.yml"):
        assert '"factory:incident"' not in job_condition(name)


def _wakeup_script() -> str:
    import textwrap

    text = (WORKFLOWS / "factory-issue-wakeup.yml").read_text()
    return textwrap.dedent(text.split("run: |\n", 1)[1])


def _wake(tmp_path: Path, pending: str) -> str:
    import os
    import subprocess

    stub = tmp_path / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == api ]]; then echo "$STUB_PENDING"; exit 0; fi\n'
        'echo "$@" >> "$STUB_LOG"\n'
    )
    stub.chmod(0o755)
    log = tmp_path / "dispatched.log"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "STUB_PENDING": pending,
        "STUB_LOG": str(log),
        "ISSUE_NUMBER": "5862",
        "GITHUB_REPOSITORY": "o/r",
    }
    subprocess.run(["bash", "-c", _wakeup_script()], env=env, check=True)
    return log.read_text() if log.exists() else ""


def test_an_issue_wakeup_sends_one_dispatch_while_one_is_still_waiting(tmp_path: Path) -> None:
    """Six new Muse QA issues fired twelve dispatches on Nov 26 and starved the roster chain."""
    assert "target=issue:5862" in _wake(tmp_path, "0")
    (tmp_path / "dispatched.log").unlink()
    assert _wake(tmp_path, "1") == ""
