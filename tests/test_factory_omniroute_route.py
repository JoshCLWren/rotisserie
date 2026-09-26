"""Tests for role-aware native OmniRoute factory routing."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "factory_omniroute_route.py"
)


def load_module() -> ModuleType:
    """Load the routing helper without packaging .github."""
    spec = importlib.util.spec_from_file_location("factory_omniroute_route", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ROUTES = load_module()


def test_issue_implementation_uses_free_coding_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    assert ROUTES.route_for_assignment("issue") == "auto/coding:free"


def test_pr_repair_uses_free_coding_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    assert (
        ROUTES.route_for_assignment("pr", "factory:changes-requested")
        == "auto/coding:free"
    )


def test_exact_head_review_uses_free_reasoning_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    assert (
        ROUTES.route_for_assignment("pr", "factory:review")
        == "auto/reasoning:free"
    )


def test_omniroute_routes_refuse_while_incident_dark(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_OMNIROUTE_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="multi-provider Entry path"):
        ROUTES.route_for_assignment("pr", "factory:review")


def test_worker_refuses_omniroute_source_during_incident() -> None:
    """OmniRoute auto routes stay dark; the worker refuses omniroute-free."""
    worker = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "scripts"
        / "free-model-factory-worker.sh"
    ).read_text(encoding="utf-8")

    assert "OmniRoute Entry is disabled for this incident" in worker
    assert "FACTORY_OMNIROUTE_ENABLED:-off" in worker
    assert "omniroute-free" in worker
    assert "OmniRoute-only" not in worker
