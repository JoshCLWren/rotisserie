"""Fast regression coverage for the executable OpenCode model-catalog preflight."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WAIT_SCRIPT = ROOT / ".github/scripts/wait-opencode-model.sh"
WORKFLOW = ROOT / ".github/workflows/free-model-factory-run.yml"


@pytest.mark.parametrize(
    ("catalog", "expected_status"),
    [
        ("vendor/selected:free\nvendor/other:free\n", 0),
        ("vendor/other:free\n", 3),
        ("", 1),
        (None, 1),
    ],
)
def test_model_preflight_distinguishes_model_absence_from_catalog_failure(
    tmp_path: Path, catalog: str | None, expected_status: int
) -> None:
    """A missing model is not a failed catalog read or a successful preflight."""
    model_command = tmp_path / "mock-opencode"
    if catalog is None:
        model_command.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    else:
        model_command.write_text(
            "#!/usr/bin/env bash\n"
            '[[ "$1" == models && "$2" == --standalone ]] || exit 2\n'
            f"printf '%b' {catalog!r}\n",
            encoding="utf-8",
        )
    model_command.chmod(0o755)

    # Simulate the end of the 45-second polling window in one iteration without
    # changing the production deadline or adding a test-only runtime flag.
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("sleep() { SECONDS=3600; }\n", encoding="utf-8")
    env = {
        **os.environ,
        "BASH_ENV": str(bash_env),
        "OPENCODE_BIN": str(model_command),
    }
    result = subprocess.run(
        ["bash", str(WAIT_SCRIPT), "vendor/selected:free"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == expected_status
    if expected_status == 0:
        assert "catalog ready" in result.stdout
    else:
        assert "did not expose" in result.stderr


WORKER = WORKFLOW.parents[1] / "scripts" / "free-model-factory-worker.sh"


def _failover_selector() -> str:
    worker = WORKER.read_text(encoding="utf-8")
    return worker.split("select_next_eligible_model() {", 1)[1].split("\n}\n", 1)[0]


def test_factory_workflow_records_precise_preflight_outcome() -> None:
    """A bad model cools down, but a catalog outage never retires its model.

    The executable-catalog check now runs inside the leased failover loop,
    right before the next real coding attempt (#5280).
    """
    selector = _failover_selector()
    assert 'bash "$FACTORY_TRUSTED_FAILOVER_DIR/wait-opencode-model.sh" "$next_runtime"' in selector
    assert "if (( candidate_status == 3 )); then" in selector
    assert "model_unavailable model" in selector
    assert "if (( candidate_status != 0 )); then" in selector
    assert "control_plane_failure" in selector


def test_catalog_preflight_hands_selected_model_directly_to_real_work() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    prework = workflow.split("      - name: Run continuous fixed-model factory session", 1)[0]
    # No pre-work model ceiling or inference probe: real work is the test.
    assert "max_candidates=5" not in prework
    assert "opencode run" not in prework
    assert "Smoke exact pinned model through OpenCode" not in workflow
    assert 'FACTORY_RUNTIME_MODEL: ${{ steps.executor.outputs.runtime_model || steps.lane.outputs.runtime_model }}' in workflow
    selector = _failover_selector()
    assert 'RUNTIME_MODEL="$next_runtime"' in selector
    assert "FACTORY_FLEET_TRANSITION" in selector


def test_model_rotation_is_bound_to_trusted_fleet_admission() -> None:
    """Cross-provider rotation only uses Neon-admitted sources and free filters."""
    selector = _failover_selector()
    assert '--eligible-sources-file "$RUNNER_TEMP/factory-fleet-sources.json"' in selector
    assert '--exclusions-file "$RUNNER_TEMP/factory-neon-model-exclusions.json"' in selector
    assert "--blocked-provider" in selector
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "export-fleet-failover-sources" in workflow
    assert "FACTORY_FLEET_SOURCES: ${{ needs.validate_assignment.outputs.fleet_sources }}" in workflow
