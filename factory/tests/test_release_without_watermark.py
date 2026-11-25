"""Releasing a label with no store claim must not need an inactivity watermark."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_work_controller", SCRIPTS / "factory-work-controller.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paired_release_without_watermark_writes_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load()
    owned = {"body": "", "state": "OPEN", "labels": [{"name": x} for x in ("factory", "factory:7")]}
    written: list[Any] = []
    monkeypatch.setattr(module, "target_json", lambda n: owned)
    monkeypatch.setattr(module, "run_gh", lambda args, **kw: written.append(kw.get("input_json")))
    released = module.replace_factory_labels(
        9,
        "factory:unowned",
        expected_owner="factory:7",
        observed_at_epoch=1_000,
        related_owner_target=10,
    )
    assert released is not False
    assert "factory:unowned" in written[0]["labels"]
    assert "factory:7" not in written[0]["labels"]
