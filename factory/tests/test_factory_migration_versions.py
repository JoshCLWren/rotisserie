"""The merge gate rejects migration versions that collide with the base (Rotisserie #366)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import factory_adopter
import factory_migration_families as families
import factory_migration_versions as versions
import pytest
from pytest import MonkeyPatch
from tests.test_factory_outside_approval import load_controller

SCRIPTS = Path(versions.__file__).parent
REPO = "o/r"
HEAD = "a" * 40
REFINERY = {
    "family": "refinery",
    "directory": "native/migrate/migrations/",
    "version": r"^V(?P<version>[0-9]+)__[A-Za-z0-9_]+\.sql$",
}
DIRECTORY = "native/migrate/migrations"


class FakeGitHub:
    def __init__(self, files: list[dict[str, Any]], base: list[str] | None, head: str = HEAD):
        self.files, self.base, self.head, self.calls = files, base, head, []

    def __call__(self, args: list[str]) -> object:
        path = args[-1]
        self.calls.append(path)
        if path == f"repos/{REPO}/pulls/7":
            return {"head": {"sha": self.head}, "base": {"ref": "main"}}
        if path.startswith(f"repos/{REPO}/pulls/7/files"):
            return [self.files]
        if path == f"repos/{REPO}/contents/{DIRECTORY}?ref=main":
            if self.base is None:
                raise RuntimeError("gh api failed (1): HTTP 404: Not Found")
            return [{"name": name, "type": "file"} for name in self.base]
        raise AssertionError(f"unexpected read {path}")


def added(name: str, status: str = "added", **extra: str) -> dict[str, str]:
    return {"filename": f"{DIRECTORY}/{name}", "status": status, **extra}


def main_through(top: int) -> list[str]:
    return [f"V{n}__step_{n}.sql" for n in range(1, top + 1)]


@pytest.fixture
def declare(monkeypatch: MonkeyPatch, tmp_path: Path):
    def write(*entries: dict[str, Any]) -> None:
        body = "".join(
            "[[migrations]]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in e.items())
            for e in entries
        )
        config = tmp_path / "config.toml"
        config.write_text(body, encoding="utf-8")
        monkeypatch.setenv("ROTISSERIE_CONFIG", str(config))
        factory_adopter.load_config.cache_clear()

    yield write
    factory_adopter.load_config.cache_clear()


def run(github: FakeGitHub) -> dict[str, str]:
    return versions.merge_gate(REPO, 7, HEAD, github)


def test_two_concurrent_v9_prs_pass_first_and_send_the_second_to_repair(declare) -> None:
    declare(REFINERY)
    assert run(FakeGitHub([added("V9__first.sql")], main_through(8)))["decision"] == "pass"
    second = run(FakeGitHub([added("V9__second.sql")], [*main_through(8), "V9__first.sql"]))
    assert second["decision"] == "deny"
    assert "already has as native/migrate/migrations/V9__first.sql" in second["reason"]
    assert "renumber it to 10" in second["reason"]


def test_distinct_new_versions_pass(declare) -> None:
    declare(REFINERY)
    github = FakeGitHub([added("V9__a.sql"), added("V10__b.sql")], main_through(8))
    assert run(github)["decision"] == "pass"


def test_one_pr_with_two_files_of_the_same_version_is_denied(declare) -> None:
    declare(REFINERY)
    result = run(FakeGitHub([added("V9__a.sql"), added("V9__b.sql")], main_through(8)))
    assert result["decision"] == "deny"
    assert "both use version 9" in result["reason"]


@pytest.mark.parametrize(("increasing", "decision"), [(True, "deny"), (False, "pass")])
def test_a_version_below_the_base_top_needs_the_increasing_option(
    declare, increasing: bool, decision: str
) -> None:
    declare({**REFINERY, "increasing": increasing})
    base = [name for name in main_through(10) if not name.startswith("V9__")]
    result = run(FakeGitHub([added("V9__late.sql")], base))
    assert result["decision"] == decision
    if increasing:
        assert "already at 10; renumber it to 11" in result["reason"]


@pytest.mark.parametrize(
    "row",
    [
        added("V3__step_3.sql", status="modified"),
        added("V3__step_3.sql", status="removed"),
        added("V12__new.sql", status="renamed", previous_filename=f"{DIRECTORY}/V3__step_3.sql"),
        added("notes.md"),
    ],
)
def test_ambiguous_migration_changes_fail_closed(declare, row: dict[str, str]) -> None:
    declare(REFINERY)
    result = run(FakeGitHub([row], main_through(8)))
    assert result["decision"] == "deny"
    assert "add a new file instead" in result["reason"]


def test_ordinary_sql_and_alembic_changes_never_list_the_base(declare) -> None:
    declare(REFINERY, {"family": "alembic", "directory": "alembic/versions/"})
    github = FakeGitHub(
        [
            {"filename": "db/seed.sql", "status": "modified"},
            {"filename": "alembic/versions/abc123_x.py", "status": "modified"},
        ],
        base=None,
    )
    assert run(github)["decision"] == "pass"
    assert not any("contents" in call for call in github.calls)


def test_alembic_only_adopters_skip_every_github_read(declare) -> None:
    declare({"family": "alembic", "directory": "alembic/versions/"})
    github = FakeGitHub([added("V9__x.sql")], main_through(8))
    assert run(github)["decision"] == "pass"
    assert github.calls == []


def test_a_first_migration_passes_when_the_base_has_no_directory(declare) -> None:
    declare(REFINERY)
    assert run(FakeGitHub([added("V1__init.sql")], base=None))["decision"] == "pass"


def test_a_moved_head_and_a_truncated_listing_hold_the_merge(declare) -> None:
    declare(REFINERY)
    moved = run(FakeGitHub([added("V9__a.sql")], main_through(8), head="b" * 40))
    assert moved["decision"] == "retry"
    full = [f"V{n}__s.sql" for n in range(1, versions.CONTENTS_LIMIT + 1)]
    assert run(FakeGitHub([added("V1001__a.sql")], full))["decision"] == "retry"


def test_an_invalid_config_holds_the_merge(declare) -> None:
    declare({**REFINERY, "version": "("})
    result = run(FakeGitHub([added("V9__a.sql")], main_through(8)))
    assert result["decision"] == "retry"
    assert "config is invalid" in result["reason"]


def test_the_mechanical_gate_routes_a_collision_to_repair(
    declare, monkeypatch: MonkeyPatch
) -> None:
    declare(REFINERY)
    ctrl = load_controller()
    passing = {"decision": "pass", "reason": "ok"}
    for name in ("poll_mergeable_gate", "required_checks_gate", "current_head_review_gate"):
        monkeypatch.setattr(ctrl, name, lambda *_a: passing)
    monkeypatch.setattr(ctrl, "declared_dependency_gate", lambda *_a: passing)
    monkeypatch.setattr(ctrl, "pr_json", lambda _n: {"labels": []})
    monkeypatch.setattr(ctrl, "REPO", REPO)
    github = FakeGitHub([added("V9__second.sql")], [*main_through(8), "V9__first.sql"])
    monkeypatch.setattr(ctrl, "gh_json", github)
    result = ctrl.mechanical_merge_gate(7, HEAD)
    assert result["decision"] == "deny"
    assert result["reason"].startswith("migration version conflict:")


@pytest.mark.parametrize(
    "worker",
    [
        "free-model-factory-worker.sh",
        "free-model-factory-worker-primitives.sh",
        "omniroute-factory-worker.sh",
        "nvidia-factory-worker.sh",
    ],
)
def test_the_check_runs_from_each_trusted_review_stage_alone(tmp_path: Path, worker: str) -> None:
    """#349 shipped an import the review stage did not copy, and every verdict crashed."""
    script = (SCRIPTS / worker).read_text(encoding="utf-8")
    block = script.split("stage_trusted_review_controller() {", 1)[1].split("\n}\n", 1)[0]
    for name in sorted(set(re.findall(r"\.github/scripts/([\w.-]+\.py)", block))):
        shutil.copy(SCRIPTS / name, tmp_path / name)
    config = tmp_path / "config.toml"
    config.write_text(
        "[[migrations]]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in REFINERY.items()),
        encoding="utf-8",
    )
    code = (
        "import json, factory_migration_versions as v\n"
        f"pr = {{'head': {{'sha': {HEAD!r}}}, 'base': {{'ref': 'main'}}}}\n"
        f"files = [[{{'filename': '{DIRECTORY}/V9__b.sql', 'status': 'added'}}]]\n"
        "base = [{'name': 'V9__a.sql'}]\n"
        "fetch = lambda a: pr if a[-1].endswith('/7') else files if '/files' in a[-1] else base\n"
        f"print(json.dumps(v.merge_gate('o/r', 7, {HEAD!r}, fetch)))"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["ROTISSERIE_CONFIG"] = str(config)
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, text=True, capture_output=True, env=env
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["decision"] == "deny"


def test_the_increasing_option_is_validated() -> None:
    with pytest.raises(factory_adopter.AdopterConfigError):
        families.families({"migrations": [{**REFINERY, "increasing": "yes"}]})
    with pytest.raises(factory_adopter.AdopterConfigError):
        families.families({"migrations": [{"family": "a", "directory": "a/", "increasing": True}]})
