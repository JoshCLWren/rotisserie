"""Regression coverage for dependency sequencing at the controller intake boundary."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location(
    "factory_work_controller_dependency_intake",
    SCRIPTS / "factory-work-controller.py",
)
assert SPEC is not None and SPEC.loader is not None
controller = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = controller
SPEC.loader.exec_module(controller)


def test_controller_fetches_issue_bodies_before_dependency_ranking(monkeypatch):
    """Production intake must preserve ``Depends on`` declarations for ranking."""
    requested_args: list[str] = []
    payload = [
        {
            "number": 100,
            "title": "Prerequisite",
            "labels": [{"name": "factory:unowned"}],
            "body": "",
            "createdAt": "2026-09-05T00:00:00Z",
            "updatedAt": "2026-09-05T00:00:00Z",
        },
        {
            "number": 200,
            "title": "Dependent",
            "labels": [{"name": "factory:unowned"}],
            "body": "Depends on #100",
            "createdAt": "2026-09-05T00:01:00Z",
            "updatedAt": "2026-09-05T00:01:00Z",
        },
    ]

    def fake_gh_json(args: list[str], *, input_json: object | None = None) -> object:
        del input_json
        requested_args[:] = args
        return payload

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)

    issues = controller.list_issues()
    json_fields = requested_args[requested_args.index("--json") + 1].split(",")
    assert "body" in json_fields

    candidates = controller.build_candidates(issues, [])
    candidate_numbers = {candidate.number for candidate in candidates}
    assert 100 in candidate_numbers
    assert 200 not in candidate_numbers
