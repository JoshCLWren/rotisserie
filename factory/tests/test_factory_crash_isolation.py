"""One item's GitHub failure never stops a pass; a programming error still fails it."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
crash = importlib.import_module("factory_crash")


@pytest.fixture(scope="module")
def controller() -> types.ModuleType:
    path = SCRIPTS / "factory-work-controller.py"
    spec = importlib.util.spec_from_file_location("factory_work_controller_isolation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "store.db"
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(path))
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    return path


def _stored(path: Path) -> list[tuple[str, int, str]]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute("SELECT kind, fatal, exception_type FROM factory_crash"))
    finally:
        conn.close()


def _stale(controller: types.ModuleType, number: int, now: int) -> dict[str, Any]:
    updated = datetime.fromtimestamp(now - controller.LOCAL_LEASE_TTL_SECONDS - 1, tz=UTC)
    names = ("factory", "factory:local", "factory:building")
    return {
        "number": number,
        "state": "OPEN",
        "updatedAt": updated.isoformat(),
        "labels": [{"name": name} for name in names],
    }


def _setup(controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, fail: object) -> int:
    now = 2_000_000_000
    rows = {number: _stale(controller, number, now) for number in (11, 12)}
    monkeypatch.setattr(controller, "list_issues", lambda: list(rows.values()))
    monkeypatch.setattr(controller, "list_prs", lambda: [])
    monkeypatch.setattr(controller, "active_fixed_workers", lambda: (set(), set()))
    monkeypatch.setattr(controller, "latest_lease_activity_epoch", lambda number: None)
    monkeypatch.setattr(
        controller, "target_json", lambda number: {**rows[number], "updated_at": None}
    )

    def write(args: list[str], *, input_json: object | None = None, check: bool = True) -> str:
        if "issues/11/labels" in " ".join(args):
            raise fail  # type: ignore[misc]
        return "{}"

    monkeypatch.setattr(controller, "run_gh", write)
    return now


def test_a_transient_label_write_failure_on_one_lease_does_not_stop_the_pass(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, store: Path
) -> None:
    failure = crash.GitHubCallError("gh api PUT labels failed (1): unexpected end of JSON input")
    now = _setup(controller, monkeypatch, failure)
    assert controller.reconcile_stale_leases(now_epoch=now) == [12]
    assert _stored(store) == [("item", 0, "GitHubCallError")]


def test_the_real_gh_wrapper_raises_the_isolated_error_type(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*_args: object, **_kwargs: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502")

    monkeypatch.setattr(controller.subprocess, "run", failed)
    with pytest.raises(crash.GitHubCallError):
        controller.run_gh(["api", "repos/acme/widgets/issues/1/labels"])


@pytest.mark.parametrize(
    "error",
    [
        UnboundLocalError("cannot access local variable 'checked_at'"),
        NameError("name 'checked_at' is not defined"),
        TypeError("unsupported operand"),
        AttributeError("'NoneType' object has no attribute 'get'"),
        KeyError("labels"),
        RuntimeError("dispatcher authorization failed: mutation blocked"),
    ],
)
def test_a_programming_error_in_one_lease_still_fails_the_pass(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, store: Path, error: Exception
) -> None:
    now = _setup(controller, monkeypatch, error)
    with pytest.raises(type(error)):
        controller.reconcile_stale_leases(now_epoch=now)


class _Store:
    def enabled(self) -> bool:
        return True

    def record(self, entries: list[dict[str, Any]]) -> int:
        return len(entries)

    def holder(self, number: int) -> str | None:
        return None

    def loop_evidence(self, seconds: int) -> tuple[dict[str, list[Any]], dict[str, Any]]:
        return {}, {}


class _GitHub:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.puts: list[str] = []

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if path.endswith("/labels"):
            if path.startswith("issues/21/"):
                raise self.error
            self.puts.append(path)
            return {}
        if path.endswith("/comments"):
            return {}
        number = int(path.split("/")[1])
        names = ["factory", "factory:unowned", "factory:changes-requested"]
        return {"number": number, "body": "", "labels": [{"name": name} for name in names]}

    def comments(self, number: int) -> list[dict[str, Any]]:
        return []

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        return {}


def _heal(monkeypatch: pytest.MonkeyPatch, error: Exception) -> tuple[_GitHub, list[Any]]:
    actions = importlib.import_module("factory_self_heal_actions")
    rules = importlib.import_module("factory_self_heal_rules")
    found = types.SimpleNamespace(
        ref="r", healed=False, model="", verdict="changes-requested", head="a" * 40
    )
    monkeypatch.setattr(rules, "empty_verdict", lambda comments, number: found)
    names = [{"name": n} for n in ("factory", "factory:unowned", rules.CHANGES_REQUESTED)]
    pulls = [{"number": n, "labels": names, "head": {"sha": "a" * 40}} for n in (21, 22)]
    github = _GitHub(error)
    budget = actions.Budget(actions=5, syncs=0)
    return github, actions.heal_empty_verdicts(github, _Store(), pulls, budget)


def test_self_heal_skips_one_pr_whose_label_write_failed(
    monkeypatch: pytest.MonkeyPatch, store: Path
) -> None:
    github, done = _heal(monkeypatch, crash.GitHubCallError("HTTP 502 on PUT labels"))
    assert github.puts == ["issues/22/labels"]
    assert [row.get("pr") for row in done] == [22]
    assert _stored(store) == [("item", 0, "GitHubCallError")]


def test_self_heal_still_fails_on_a_programming_error(
    monkeypatch: pytest.MonkeyPatch, store: Path
) -> None:
    with pytest.raises(AttributeError):
        _heal(monkeypatch, AttributeError("'NoneType' object has no attribute 'get'"))


def test_the_self_heal_gh_client_raises_the_isolated_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heal = importlib.import_module("factory_self_heal")

    def failed(*_args: object, **_kwargs: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502")

    monkeypatch.setattr(heal.subprocess, "run", failed)
    with pytest.raises(crash.GitHubCallError, match="HTTP 502"):
        heal.GhCli("acme/widgets").api("issues/1/labels")
