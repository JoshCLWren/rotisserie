"""A pinned model that is unavailable falls back inside its provider unless the pin is exact."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"
WORKFLOW = ROOT.parent / ".github/workflows/free-model-factory-run.yml"
sys.path.insert(0, str(SCRIPTS))

import factory_pinned_model as pinned  # noqa: E402

PINNED = "nvidia/meta/muse-glimmer-30b"
STUB = textwrap.dedent(
    """\
    import json, os, sys
    name = os.path.basename(sys.argv[0])
    args = sys.argv[1:]
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(json.dumps([name, args]) + "\\n")
    if name == "factory_manual_model.py":
        print("Exact model unavailable: Exact model missing from current provider catalog",
              file=sys.stderr)
        sys.exit(1)
    if name == "factory_runtime_model_select.py":
        pick = os.environ.get("STUB_PICK", "")
        if not pick:
            sys.exit(1)
        print(json.dumps({"source": "nvidia", "model": pick, "runtime_model": "nvidia/" + pick}))
        sys.exit(0)
    if name == "factory_model_failover.py":
        if "--proven-only" in args or not os.environ.get("STUB_REPICK"):
            sys.exit(1)
        pick = os.environ["STUB_REPICK"]
        print(json.dumps({"source": "nvidia", "model": pick, "runtime_model": "nvidia/" + pick}))
        sys.exit(0)
    if name == "factory_review_model_guard.py":
        model = args[args.index("--model") + 1]
        authors = json.loads(os.environ.get("FACTORY_REVIEW_AUTHORS", "[]"))
        if model in authors:
            print(model + " authored this PR")
            sys.exit(0)
        sys.exit(1)
    sys.exit(0)
    """
)


def _selector() -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    step = next(
        step
        for step in jobs["factory"]["steps"]
        if step.get("name") == "Select execution candidate at dispatch time"
    )
    return step["run"]


def _run(tmp_path: Path, *, exact: bool = False, pick: str = "meta/muse-glow-70b", **env: str):
    scripts = tmp_path / ".github/scripts"
    scripts.mkdir(parents=True)
    for name in (
        "factory_manual_model.py",
        "factory_runtime_model_select.py",
        "factory_model_failover.py",
        "factory_review_model_guard.py",
    ):
        (scripts / name).write_text(STUB, encoding="utf-8")
    (scripts / "factory_provider_registry.py").write_text("PROVIDERS = {'nvidia': None}\n")
    (scripts / "factory_pinned_model.py").write_text(
        (SCRIPTS / "factory_pinned_model.py").read_text(encoding="utf-8")
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$GH_LOG"\n')
    gh.chmod(0o755)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    assignment = {"kind": "pr", "number": 5674, "worker": "1236998703302"}
    if exact:
        assignment["exact_model"] = True
    base = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_REPOSITORY": "owner/product",
        "GITHUB_RUN_ID": "45464199744",
        "STUB_LOG": str(tmp_path / "stub.log"),
        "GH_LOG": str(tmp_path / "gh.log"),
        "STUB_PICK": pick,
        "WORKER": "1236998703302",
        "SOURCE": "nvidia",
        "LANE_BRANCH_SUFFIX": "nvidia",
        "REQUESTED_MODEL": PINNED,
        "REQUESTED_PROVIDER": "nvidia",
        "FACTORY_ASSIGNMENT": json.dumps(assignment),
        "FACTORY_MODEL_EXCLUSIONS": "{}",
        "FACTORY_FLEET_SOURCES": '["nvidia"]',
        **env,
    }
    result = subprocess.run(
        ["bash", "-c", _selector()],
        cwd=tmp_path,
        env=base,
        capture_output=True,
        text=True,
        check=False,
    )
    output = tmp_path / "output"
    values = dict(
        line.split("=", 1)
        for line in (output.read_text().splitlines() if output.exists() else [])
        if "=" in line
    )
    return result, values


def test_unavailable_pin_uses_the_best_eligible_model_of_the_same_provider(tmp_path):
    result, outputs = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["source"] == "nvidia"
    assert outputs["runtime_model"] == "nvidia/meta/muse-glow-70b"
    assert outputs["requested_model"] == PINNED
    assert outputs["used_model"] == "nvidia/meta/muse-glow-70b"
    assert "was unavailable" in outputs["model_note"]
    calls = [json.loads(line) for line in (tmp_path / "stub.log").read_text().splitlines()]
    select = next(args for name, args in calls if name == "factory_runtime_model_select.py")
    assert select[select.index("--source") + 1] == "nvidia"
    assert "--exclusions-file" in select
    summary = (tmp_path / "summary").read_text()
    assert PINNED in summary and "nvidia/meta/muse-glow-70b" in summary
    assert not (tmp_path / "gh.log").exists()
    note = (tmp_path / "runner/factory-model-fallback.md").read_text()
    assert note.startswith("**Factory model fallback:** Pinned model " + PINNED)


def test_the_fallback_note_is_posted_after_the_session_and_cannot_gate_it():
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["factory"]["steps"]
    names = [step.get("name") for step in steps]
    note = steps[names.index("Note the pinned model fallback on the target")]
    assert names.index("Run continuous fixed-model factory session") < names.index(note["name"])
    assert note["continue-on-error"] is True
    assert note["if"] == "always() && steps.executor.outputs.model_note != ''"
    assert "issues/${target}/comments" in note["run"]
    assert "factory-model-fallback.md" in note["run"]


def test_exact_pin_keeps_the_hard_failure(tmp_path):
    result, outputs = _run(tmp_path, exact=True)
    assert result.returncode == 1
    assert pinned.EXACT_ERROR in result.stdout
    outcome = (tmp_path / "runner/factory-discovery-outcome").read_text()
    assert outcome.startswith("model_unavailable\t")
    assert "runtime_model" not in outputs


def test_a_provider_with_no_eligible_model_is_no_capacity(tmp_path):
    result, _ = _run(tmp_path, pick="")
    assert result.returncode == 1
    outcome = (tmp_path / "runner/factory-discovery-outcome").read_text()
    assert outcome.startswith("no_capacity\t")
    assert PINNED in outcome
    assert pinned.EXACT_ERROR not in result.stdout


def test_fallback_pick_still_obeys_the_review_author_rule(tmp_path):
    result, outputs = _run(
        tmp_path,
        FACTORY_REVIEW_AUTHORS='["nvidia/meta/muse-glow-70b"]',
        STUB_REPICK="meta/muse-sky-120b",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["runtime_model"] == "nvidia/meta/muse-sky-120b"
    calls = [json.loads(line) for line in (tmp_path / "stub.log").read_text().splitlines()]
    repick = [args for name, args in calls if name == "factory_model_failover.py"][-1]
    assert "--eligible-sources-file" not in repick
    assert "--attempted" in repick


def test_decisions() -> None:
    assert pinned.split_request(f"exact:{PINNED}") == (PINNED, True)
    assert pinned.split_request(PINNED) == (PINNED, False)
    assert pinned.exact_from_assignment('{"exact_model": true}') is True
    assert pinned.exact_from_assignment('{"exact_model": "true"}') is False
    assert pinned.exact_from_assignment("not json") is False
    assert pinned.unavailable(PINNED, "Exact model unavailable: gone", exact=False) == {
        "action": "fallback",
        "reason": "gone",
        "requested": PINNED,
    }
    assert pinned.unavailable(PINNED, "gone", exact=True)["error"] == pinned.EXACT_ERROR
    assert pinned.note("auto", "nvidia/x", "") == ""


def test_dispatch_marks_an_exact_prefix_as_exact_or_nothing(monkeypatch):
    import factory_completion_controller as completion
    import factory_worker_slots as worker_slots

    spec = importlib.util.spec_from_file_location(
        "factory_work_controller", SCRIPTS / "factory-work-controller.py"
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "factory_work_controller", controller)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(
        worker_slots, "slots", lambda: [SimpleNamespace(source="nvidia", index=0, worker="1")]
    )
    monkeypatch.setattr(completion, "durable_factory_state", lambda: ({}, {}))
    monkeypatch.setattr(completion, "load_manifest_candidates", lambda: [])
    monkeypatch.setattr(
        completion,
        "capacity_report",
        lambda *_a, **_kw: {"executable_candidates": [{"worker": "1"}]},
    )
    monkeypatch.setattr(
        controller,
        "assign",
        lambda worker, named=False: SimpleNamespace(
            kind="issue", number=7, stage="factory:building", conflicted=False
        ),
    )
    monkeypatch.setattr(
        controller, "assignment_context", lambda c, worker: {"worker": worker, "number": c.number}
    )
    launched: list[list[str]] = []
    monkeypatch.setattr(controller, "run_gh", lambda argv: launched.append(argv))

    def sent(model: str) -> tuple[dict[str, object], str]:
        launched.clear()
        controller.dispatch_next("nvidia", model)
        argv = launched[0]
        fields = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-f")
        return json.loads(fields["assignment"]), fields["model"]

    assignment, model = sent(f"exact:{PINNED}")
    assert assignment["exact_model"] is True
    assert model == PINNED
    assignment, model = sent(PINNED)
    assert "exact_model" not in assignment
    assert model == PINNED
