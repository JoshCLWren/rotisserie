from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


@pytest.fixture(autouse=True)
def _no_person_reopen(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Closure tests here describe issues no person reopened."""
    if request.node.module.__name__.rsplit(".", 1)[-1] != "test_factory_implementation_delivery":
        return
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    github = importlib.import_module("factory_post_merge_closure_github")
    monkeypatch.setattr(github, "_reopened_by_person", lambda _number: False)
