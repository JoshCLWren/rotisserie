"""Contract tests for the read-only Rotisserie adopter boundary."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "factory_rotisserie_adopter.py"
FIXTURE = ROOT / "tests" / "fixtures" / "factory-rotisserie" / "parity-view.json"


def load_adapter() -> ModuleType:
    """Load the workflow-side adapter as a testable module."""
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    
    # Use importlib.import_module instead of util.spec_from_file_location
    # since util may not be available in all Python environments
    sys.path.insert(0, str(SCRIPT.parent))
    module = importlib.import_module("factory_rotisserie_adopter")
    return module


def test_one_capture_generates_both_versioned_shadow_inputs() -> None:
    adapter = load_adapter()
    raw = FIXTURE.read_bytes()

    bundle = adapter.build_bundle(raw)

    assert bundle["source_revision"] == "3c37115284352561c8c260f6e0932aa2addb8e12"
    assert len(bundle["source_sha256"]) == 64
    legacy = bundle["legacy"]
    snapshot = bundle["snapshot"]
    assert legacy["schema_version"] == 1
    assert legacy["revision"] == bundle["source_revision"]
    assert set(legacy["dimensions"]) == {
        "eligibility", "ranking", "ownership", "review", "completion", "recovery"
    }
    assert snapshot["schema_version"] == 1
    assert {item["id"]["repository"]["name"] for item in snapshot["works"]} == {"comic-pile"}


def test_fixture_covers_exact_head_review_completion_and_lease_recovery() -> None:
    adapter = load_adapter()
    view = json.loads(FIXTURE.read_text(encoding="utf-8"))

    legacy = adapter.legacy_decisions(view)
    observations = {
        (item["dimension"], item["subject"]): item for item in legacy["observations"]
    }

    assert observations[("eligibility", "work:3002")]["outcome"] == "eligible"
    assert observations[("eligibility", "work:3003")]["reasons"] == [
        "implementation_exists"
    ]
    assert observations[("review", "change:4001")]["outcome"] == "approved"
    assert observations[("completion", "change:4001")]["outcome"] == "ready"
    assert observations[("recovery", "lease:issue-3004-7")]["outcome"] == "retain"

    view["captured_at"] = 1790612100
    expired = adapter.legacy_decisions(view)
    expired_observations = {
        (item["dimension"], item["subject"]): item for item in expired["observations"]
    }
    assert expired_observations[("recovery", "lease:issue-3004-7")]["outcome"] == "release"


def test_changed_head_cannot_reuse_old_review_evidence() -> None:
    adapter = load_adapter()
    view = json.loads(FIXTURE.read_text(encoding="utf-8"))
    pr = view["pull_requests"][0]
    pr["reviews"][0]["head"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    snapshot = adapter.graph_snapshot(view)
    legacy = adapter.legacy_decisions(view)
    observations = {
        (item["dimension"], item["subject"]): item for item in legacy["observations"]
    }

    assert snapshot["reviews"][0]["revision"]["value"] != snapshot["changes"][0]["head"]["value"]
    assert observations[("review", "change:4001")]["outcome"] == "pending"
    assert observations[("completion", "change:4001")]["outcome"] == "blocked"


def test_rejects_an_unscoped_repository_capture() -> None:
    adapter = load_adapter()
    view = json.loads(FIXTURE.read_text(encoding="utf-8"))
    view["repository"] = "someone/else"

    try:
        adapter.graph_snapshot(view)
    except adapter.AdopterInputError as error:
        assert "outside" in str(error)
    else:
        raise AssertionError("unscoped repository capture was accepted")


def test_retry_suppression_is_visible() -> None:
    adapter = load_adapter()
    view = json.loads(FIXTURE.read_text(encoding="utf-8"))
    
    # Add scripts directory to path to import factory_work_policy
    scripts_dir = Path(__file__).resolve().parents[1] / ".github" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    factory_work_policy = importlib.import_module("factory_work_policy")

    view["no_diff_attempts_by_issue"] = {"3002": factory_work_policy.FACTORY_NO_DIFF_RETRY_LIMIT}

    legacy = adapter.legacy_decisions(view)
    observations = {
        (item["dimension"], item["subject"]): item for item in legacy["observations"]
    }

    assert observations[("eligibility", "work:3002")]["outcome"] == "blocked"
    assert observations[("eligibility", "work:3002")]["reasons"] == ["adopter_policy"]
    assert ("ranking", "work:3002") not in observations


def test_recorded_repairer_cannot_authorize_a_head_it_repaired() -> None:
    adapter = load_adapter()
    view = json.loads(FIXTURE.read_text(encoding="utf-8"))
    pr = view["pull_requests"][0]
    pr["head_contributors"] = ["8", "9"]
    pr["reviews"] = [{"reviewer": "9", "decision": "approved", "required": True}]

    legacy = adapter.legacy_decisions(view)
    observations = {
        (item["dimension"], item["subject"]): item for item in legacy["observations"]
    }

    assert observations[("review", "change:4001")]["outcome"] == "blocked"
    assert observations[("completion", "change:4001")]["outcome"] == "blocked"


def test_missing_contributor_provenance_requires_two_reviewers() -> None:
    adapter = load_adapter()
    view = json.loads(FIXTURE.read_text(encoding="utf-8"))
    pr = view["pull_requests"][0]
    pr["head_contributors"] = []
    pr["reviews"] = [{"reviewer": "9", "decision": "approved", "required": True}]

    single = adapter.legacy_decisions(view)
    observations = {
        (item["dimension"], item["subject"]): item for item in single["observations"]
    }
    assert observations[("review", "change:4001")]["outcome"] == "blocked"

    pr["reviews"] = [
        {"reviewer": "9", "decision": "approved", "required": True},
        {"reviewer": "10", "decision": "approved", "required": True},
    ]
    two_reviewers = adapter.legacy_decisions(view)
    observations = {
        (item["dimension"], item["subject"]): item for item in two_reviewers["observations"]
    }
    assert observations[("review", "change:4001")]["outcome"] == "approved"


def test_projection_pressure_uses_factory_wip_policy() -> None:
    adapter = load_adapter()
    view = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert adapter.factory_pr_wip_count(view["pull_requests"]) == 0
    assert adapter.FACTORY_PR_WIP_LIMIT >= 8


def test_graph_priorities_preserve_comicpile_provenance_order() -> None:
    adapter = load_adapter()
    view = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first, second = view["issues"][:2]
    first["labels"] = ["bug", "factory", "factory:unowned", "user-reported"]
    first["createdAt"] = "2026-09-02T00:00:00Z"
    second["labels"] = [
        "factory",
        "factory:unowned",
        "infrastructure",
        "ralph-task",
        "ralph-priority:critical",
    ]
    second["createdAt"] = "2026-09-01T00:00:00Z"

    snapshot = adapter.graph_snapshot(view)
    priorities = {
        int(work["id"]["key"]): work["priority"] for work in snapshot["works"]
    }

    assert priorities[int(first["number"])] > priorities[int(second["number"])]


def test_manual_gate_mapping() -> None:
    adapter = load_adapter()
    view = json.loads(FIXTURE.read_text(encoding="utf-8"))
    view["issues"][1]["body"] = "<!-- factory-execution:manual-only -->"

    legacy = adapter.legacy_decisions(view)
    snapshot = adapter.graph_snapshot(view)
    observations = {
        (item["dimension"], item["subject"]): item for item in legacy["observations"]
    }

    assert observations[("eligibility", "work:3002")]["reasons"] == ["human_boundary"]
    assert snapshot["boundaries"] == [
        {
            "id": "comic-pile-policy-3002",
            "work": snapshot["works"][1]["id"],
            "kind": "human_approval",
            "satisfied_by": None,
        }
    ]


def test_human_gate_uses_the_structured_manual_only_contract() -> None:
    """Discussing the marker in prose must not fabricate a human gate."""
    adapter = load_adapter()

    def issue(body: str) -> dict[str, object]:
        return {"number": 9001, "body": body, "labels": []}

    assert adapter._human_gate(issue("<!-- factory-execution:manual-only -->\nGate.")) is True
    assert adapter._human_gate(issue("Gate.\nmanual-only\n")) is True
    assert adapter._human_gate(issue("Gate: <!-- factory-execution:manual-only -->")) is False
    assert adapter._human_gate(issue("The `<!-- factory-execution:manual-only -->` marker.")) is False
    assert adapter._human_gate(issue("The `manual-only` marker.")) is False
