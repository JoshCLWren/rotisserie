"""Factory label writers drop factory:available when they move an item (#191)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


def load(filename: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


START = ["factory", "factory:unowned", "factory:review", "factory:available", "bug"]


def test_review_controller_writer_drops_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load("factory-review-controller.py", "writer_review_controller")
    written: list[Any] = []
    monkeypatch.setattr(
        module, "target_json", lambda n: {"body": "", "labels": [{"name": x} for x in START]}
    )
    monkeypatch.setattr(module, "run_gh", lambda args, **kw: written.append(kw["input_json"]))
    module.replace_factory_labels(9, "factory:unowned", "factory:ci")
    assert written[0]["labels"] == ["bug", "factory", "factory:ci", "factory:unowned"]


def test_work_controller_writer_drops_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load("factory-work-controller.py", "writer_work_controller")
    written: list[Any] = []
    monkeypatch.setattr(
        module, "target_json", lambda n: {"body": "", "labels": [{"name": x} for x in START]}
    )
    monkeypatch.setattr(module, "run_gh", lambda args, **kw: written.append(kw.get("input_json")))
    module.replace_factory_labels(9, "factory:3", "factory:building")
    assert "factory:available" not in written[0]["labels"]
    assert {"bug", "factory:3", "factory:building"} <= set(written[0]["labels"])


def test_worker_shell_writer_drops_availability(tmp_path: Path) -> None:
    gh = tmp_path / "gh"
    log = tmp_path / "put.json"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \"$*\" == *PUT* ]]; then cat > {log}; else echo '{json.dumps(START)}'; fi\n"
    )
    gh.chmod(0o755)
    script = (SCRIPTS / "free-model-factory-worker-primitives.sh").read_text()
    patterns = [line for line in script.splitlines() if line.startswith(("OWNER_RE=", "STAGE_RE="))]
    function = "replace_labels() {" + script.split("\nreplace_labels() {", 1)[1].split("\n}\n", 1)[0]
    program = "\n".join([*patterns, function + "\n}", "replace_labels 9 factory:unowned factory:ci"])
    subprocess.run(
        ["bash", "-c", program],
        check=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin:/opt/homebrew/bin", "GITHUB_REPOSITORY": "acme/widgets"},
    )
    labels = json.loads(log.read_text())["labels"]
    assert "factory:available" not in labels
    assert {"bug", "factory:ci", "factory:unowned"} <= set(labels)
