"""Run-scoped model choice and bounded startup jitter (#201)."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from factory_model_failover import select_next, select_next_fleet  # noqa: E402
from factory_opencode_go import choose as go_choose  # noqa: E402
from factory_published_model_catalog import choose_model  # noqa: E402
from factory_runtime_model_select import select as runtime_select  # noqa: E402
from factory_kilo_catalog import choose as kilo_choose  # noqa: E402


def catalog(*models: str) -> dict:
    return {"data": [{"id": model, "supported_parameters": ["tools"]} for model in models]}


def test_same_worker_different_runs_diversify_initial_and_failover() -> None:
    names = [f"example/model-{i}" for i in range(5)]
    data = catalog(*names)
    first = select_next("nvidia", "41", set(), {}, catalog_json=data, rotation=201)
    assert first == select_next("nvidia", "41", set(), {}, catalog_json=data, rotation=201)
    assert len({
        select_next("nvidia", "41", set(), {}, catalog_json=data, rotation=run)["model"]
        for run in range(30)
    }) > 1
    assert len({
        runtime_select("nvidia", "41", data, rotation=run)["model"]
        for run in range(30)
    }) > 1
    fleet = {"nvidia": data}
    assert len({
        select_next_fleet("41", "nvidia", set(), set(), {}, fleet, {"nvidia"},
                          rotation=run, proven=set())["model"]
        for run in range(30)
    }) > 1
    assert select_next("nvidia", "41", set(), {"nvidia": names[:-1]},
                       catalog_json=data, rotation=999)["model"] == names[-1]


def test_published_and_kilo_picks_change_across_runs_without_skipping_exclusions() -> None:
    free = [f"example/model-{n}:free" for n in range(5)]
    data = catalog(*free)
    assert len({
        choose_model("openrouter-free", "41", data, rotation=run)
        for run in range(30)
    }) > 1
    assert choose_model("openrouter-free", "41", data, rotation=5) == (
        choose_model("openrouter-free", "41", data, rotation=5))
    assert choose_model("openrouter-free", "41", data, set(free[:-1]), 10) == free[-1]
    kilo = [f"kilo-auto/generated-{i}" for i in range(4)]
    text = "\n".join(kilo)
    assert len({kilo_choose(text, "41", rotation=run)["model"]
                for run in range(30)}) > 1
    assert kilo_choose(text, "41", set(kilo[:-1]), 9)["model"] == kilo[-1]


def test_go_does_not_pin_one_favorite_automatically_but_exact_pin_remains() -> None:
    def free_model() -> dict:
        return {
            "type": "chat",
            "capabilities": {"tools": {"supported": True}},
            "cost": {"text": {"input": {"price": 0}, "output": {"price": 0}}},
        }
    data = {"opencode-go": {"models": {
        "hy4-flash-free": free_model(),
        "other-free": free_model(),
    }}}
    picks = {go_choose(data, "41", rotation=run)["model"] for run in range(30)}
    assert picks == {"hy4-flash-free", "other-free"}
    pinned = go_choose(data, "41", rotation=17, required_model="opencode-go/hy4-flash-free")
    assert pinned["runtime_model"] == "opencode-go/hy4-flash-free"


def test_workflow_passes_run_identity_to_all_initial_and_failover_selectors() -> None:
    workflow = (ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    worker = (ROOT / ".github/scripts/free-model-factory-worker.sh").read_text()
    step = workflow.split("- name: Select execution candidate at dispatch time", 1)[1]
    step = step.split("- name: Configure selected OmniRoute capacity", 1)[0]
    for selector in (
        "factory_model_failover.py", "factory_opencode_go.py",
        "factory_kilo_catalog.py", "factory_published_model_catalog.py",
        "factory_runtime_model_select.py",
    ):
        assert selector in step
    assert step.count("GITHUB_RUN_ID:-0") >= 9
    assert 'GITHUB_RUN_NUMBER:-0' not in step
    assert '--rotation "${GITHUB_RUN_ID:-0}"' in worker


def test_jitter_is_before_attempt_marker_and_skips_pin_and_retry() -> None:
    worker = (ROOT / ".github/scripts/free-model-factory-worker.sh").read_text()
    fn = re.search(r"(?ms)^factory_inference_jitter\(\) \{\n.*?^\}", worker)
    assert fn is not None
    assert "record_terminal_outcome" not in fn.group()
    assert "record_model_failover_failure" not in fn.group()
    assert worker.index("  factory_inference_jitter\n  log \"FACTORY_EXECUTION_CANDIDATE") > fn.end()
    script = (
        fn.group() + "\n"
        + 'log() { echo "$*"; }; sleep() { echo "SLEEP:$1"; }; '
        + 'agent_attempt=1; FACTORY_EXACT_MODEL=auto; FACTORY_EXACT_SOURCE=auto; '
        + 'factory_inference_jitter; '
        + 'agent_attempt=2; factory_inference_jitter; '
        + 'agent_attempt=1; FACTORY_EXACT_MODEL=opencode-go/manual; factory_inference_jitter;\n'
    )
    result = subprocess.run(["bash", "-c", script], check=True, text=True,
                            capture_output=True)
    lines = result.stdout.splitlines()
    delays = [float(line.removeprefix("SLEEP:")) for line in lines
              if line.startswith("SLEEP:")]
    assert len(delays) == 1
    assert 0 <= delays[0] <= 5
    assert sum("FACTORY_INFERENCE_JITTER:" in line for line in lines) == 1
