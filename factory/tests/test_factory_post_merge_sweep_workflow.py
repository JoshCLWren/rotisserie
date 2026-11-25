"""The stranded-closure sweep runs on every drain tick, whatever main's state."""

from __future__ import annotations

from typing import Any

import yaml
from tests.workflow_files import REUSABLE

SWEEP = "Close issues of recently merged factory PRs"


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load((REUSABLE / "factory-ready-merge-drain.yml").read_text())
    return workflow["jobs"]["merge-ready"]["steps"]


def test_the_sweep_has_its_own_ungated_step_before_the_main_health_gate() -> None:
    steps = _steps()
    names = [step.get("name") for step in steps]
    sweep = steps[names.index(SWEEP)]
    assert "if" not in sweep
    assert "factory_post_merge_closure.py --sweep" in sweep["run"]
    assert names.index(SWEEP) < names.index("Check main health before merging")


def test_the_gated_drain_step_no_longer_holds_the_sweep() -> None:
    drain = next(s for s in _steps() if s.get("name") == "Drain exact-head ready factory PRs")
    assert "--sweep" not in drain["run"]
    assert 'python3 "$closure_script" --pr "$pr"' in drain["run"]
