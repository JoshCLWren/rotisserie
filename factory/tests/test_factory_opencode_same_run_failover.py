"""The actual leased work session replaces standalone model smoke calls."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/free-model-factory-run.yml"
SELECTOR = ROOT / ".github/scripts/factory_live_opencode.py"


def test_catalog_retry_is_bounded_once_per_pair_within_the_lease() -> None:
    """Each eligible pair is attempted at most once per assignment (#5280)."""
    worker = (ROOT / ".github/scripts/free-model-factory-worker.sh").read_text()
    selector = worker.split("select_next_eligible_model() {", 1)[1].split("\n}\n", 1)[0]
    assert 'attempted_pairs+=("$pair")' in selector
    assert 'select_args+=(--attempted-pair "$pair")' in selector
    assert "(( $(remaining) > 540 )) || return 1" in selector
    assert "--exclusions-file" in selector


def test_actual_work_is_first_inference_for_all_factory_sources() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["factory"]["steps"]
    session_indices = [
        i for i, step in enumerate(steps)
        if step.get("name") == "Run continuous fixed-model factory session"
    ]
    assert len(session_indices) == 1
    session_index = session_indices[0]
    assert "free-model-factory-worker.sh" in steps[session_index]["run"]
    for step in steps[:session_index]:
        command = str(step.get("run") or "")
        assert "opencode run" not in command
        assert "kilo-auto-factory-run.sh" not in command
        assert "/chat/completions" not in command
        assert "KILO_AUTO_FREE_OK" not in command
        assert "FIXED_MODEL_OPENCODE_OK" not in command
    assert "steps.smoke.outputs" not in WORKFLOW.read_text()


def test_selected_catalog_model_flows_to_real_work_session() -> None:
    workflow = WORKFLOW.read_text()
    assert 'FACTORY_MODEL: ${{ steps.executor.outputs.model || steps.lane.outputs.model }}' in workflow
    assert 'FACTORY_RUNTIME_MODEL: ${{ steps.executor.outputs.runtime_model || steps.lane.outputs.runtime_model }}' in workflow


def test_selector_supports_per_run_exclusions_without_paid_fallback() -> None:
    source = SELECTOR.read_text()
    assert 'parser.add_argument("--attempted", action="append", default=[])' in source
    assert 'and model not in (attempted or set())' in source
    assert 'attempted=set(args.attempted)' in source
