"""Regression coverage for the product-only main health signal."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from tests.workflow_files import adopter_view

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "factory_main_health.py"
WORKFLOWS = REPO_ROOT.parent / ".github" / "workflows"
SHA = "a" * 40


def load_module() -> ModuleType:
    """Load the main-health helper from its workflow script path.

    Returns:
        Imported helper module.

    """
    spec = importlib.util.spec_from_file_location("factory_main_health", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(
    run_id: int,
    *,
    name: str = "CI",
    event: str = "push",
    head_sha: str = SHA,
    conclusion: str = "success",
) -> dict[str, object]:
    """Build one workflow-run fixture.

    Args:
        run_id: Numeric workflow-run identifier.
        name: Workflow display name.
        event: Triggering GitHub event.
        head_sha: Commit associated with the run.
        conclusion: Terminal workflow conclusion.

    Returns:
        Workflow-run fixture.

    """
    return {
        "conclusion": conclusion,
        "event": event,
        "head_sha": head_sha,
        "id": run_id,
        "name": name,
    }


def test_factory_failures_do_not_mark_product_main_red() -> None:
    """Factory workflow failures cannot contaminate product CI health."""
    module = load_module()
    payload = {
        "workflow_runs": [
            run(
                1,
                name="Fixed Model Factory Entry",
                event="workflow_dispatch",
                conclusion="failure",
            ),
            run(
                2,
                name="Factory Ready Merge Drain",
                event="workflow_dispatch",
                conclusion="failure",
            ),
            run(3),
        ]
    }

    assert module.summarize(payload, SHA) == {
        "failing": 0,
        "failing_names": "",
        "total": 1,
    }


def test_failed_product_ci_marks_main_red_across_pages() -> None:
    """A failed exact-SHA push CI run remains blocking across pagination."""
    module = load_module()
    payload = [
        {"workflow_runs": [run(11), run(12, head_sha="b" * 40, conclusion="failure")]},
        {"workflow_runs": [run(13, conclusion="failure")]},
    ]

    assert module.summarize(payload, SHA) == {
        "failing": 1,
        "failing_names": "CI run 13 (failure)",
        "total": 2,
    }


def test_duplicate_paginated_runs_are_counted_once() -> None:
    """Overlapping REST pages cannot duplicate a main-health failure."""
    module = load_module()
    failed = run(21, conclusion="timed_out")

    assert module.summarize(
        [{"workflow_runs": [failed]}, {"workflow_runs": [failed]}],
        SHA,
    ) == {
        "failing": 1,
        "failing_names": "CI run 21 (timed_out)",
        "total": 1,
    }


def test_held_workflow_recovery_is_unbounded_and_observable() -> None:
    """Held PR workflows remain recoverable after their first hour."""
    drain = adopter_view("factory-ready-merge-drain.yml")
    guard = adopter_view("fixed-model-pr-repair-guard.yml")

    assert "actions/runs?status=action_required&per_page=100" in drain
    assert "gh api --paginate --slurp" in drain
    assert "now - 3600" not in drain
    assert "Approval errors:" in drain
    assert 'gh workflow run "$workflow_file" --ref "$branch"' in drain
    assert "factory_drain_run_presence.py" in drain
    presence = (REPO_ROOT / ".github/scripts/factory_drain_run_presence.py").read_text()
    assert "actions/workflows/{workflow}/runs?head_sha={sha}&per_page=1" in presence
    assert '"ci.yml", "CI"' in presence and "fixed-model-pr-repair-guard.yml" in presence
    assert "commits/${open_head}/check-runs" not in drain
    assert "workflow_dispatch:" in guard


def test_merge_drain_can_approve_and_dispatch_workflow_recovery() -> None:
    """The drain token has the least privilege needed for Actions recovery."""
    drain = adopter_view("factory-ready-merge-drain.yml")

    permissions = drain.split("permissions:", maxsplit=1)[1].split(
        "concurrency:", maxsplit=1
    )[0]
    assert "actions: write" in permissions
    assert 'GH_TOKEN="$FALLBACK_GH_TOKEN" gh workflow run' in drain
    assert "Dispatch errors for PR #${pr}" in drain


def test_held_run_recovery_covers_every_factory_stage() -> None:
    """Changes-requested PRs whose CI is held must still get CI approved.

    The work controller treats missing required checks as "waiting", so a
    bot-opened PR moved to changes-requested before approval would deadlock.
    """
    import json
    import re
    import subprocess

    drain = adopter_view("factory-ready-merge-drain.yml")
    step = drain.split("Recover held or missing workflow runs on factory PR heads", 1)[1]
    match = re.search(r'pulls\?state=open&per_page=100" \\\s*--jq \'([^\']+)\'', step)
    assert match is not None
    pulls = [
        {"number": n, "head": {"sha": f"sha{n}", "ref": f"b{n}"},
         "labels": [{"name": name} for name in labels]}
        for n, labels in [
            (1, ["factory", "factory:changes-requested"]),
            (2, ["factory", "factory:review"]),
            (3, ["factory", "factory:ci"]),
            (4, ["bug"]),
            (5, ["factory:ready"]),
        ]
    ]
    result = subprocess.run(
        ["jq", "-r", match.group(1)], input=json.dumps(pulls),
        text=True, capture_output=True, check=True,
    )
    assert [line.split("\t")[1] for line in result.stdout.splitlines()] == ["1", "2", "3"]
