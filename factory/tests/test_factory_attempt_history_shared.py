"""Retry history carries from one dispatch job to the next through one store row."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"
NOW = 2_000_000_000
HEAD = "a" * 40
sys.path.insert(0, str(SCRIPTS))

core = importlib.import_module("factory_turso_core")
cursors = importlib.import_module("factory_turso_cursors")


def _load() -> ModuleType:
    name = "factory_history_shared_controller"
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "factory-work-controller.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONTROLLER = _load()


def _stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _attempt(identity: int, epoch: int) -> dict[str, object]:
    prefix = CONTROLLER.MARKER_PREFIX
    return {
        "id": identity,
        "author_association": "OWNER",
        "updated_at": _stamp(epoch),
        "body": (
            f"Released.\n<!-- {prefix}-factory-claim-released-v3:pr-2264:worker:"
            f"{epoch}:review-agent-failed:sha={HEAD}:stage=factory:review:conflicted=0 -->"
        ),
    }


def _ordinary(identity: int) -> dict[str, object]:
    return {
        "id": identity,
        "author_association": "NONE",
        "body": "ordinary comment",
        "updated_at": _stamp(NOW - 50),
    }


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(tmp_path / "store.sqlite"))
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    core.use_store(None)
    yield core.store()
    core.use_store(None)


class GitHub:
    def __init__(self, responses: list[list[list[dict[str, object]]]]) -> None:
        self.responses = responses
        self.pages: list[int] = []
        self.since: list[int] = []

    def __call__(self, args: list[str]) -> object:
        query = parse_qs(urlsplit(args[-1]).query)
        self.since.append(int(datetime.fromisoformat(query["since"][0]).timestamp()))
        response = self.responses.pop(0)
        self.pages.append(len(response))
        return response


def _job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, now: int) -> list[object]:
    monkeypatch.setenv("FACTORY_ATTEMPT_HISTORY_CACHE", str(tmp_path / f"{name}.json"))
    monkeypatch.setattr(CONTROLLER.time, "time", lambda: now)
    return CONTROLLER.load_no_diff_attempt_records(now)


def _full_history() -> list[list[dict[str, object]]]:
    pages = [[_ordinary(page * 100 + row + 1) for row in range(100)] for page in range(12)]
    pages[0][:2] = [_attempt(1, NOW - 10), _attempt(2, NOW - 9)]
    return pages


def test_the_next_job_reads_one_delta_page_instead_of_the_whole_window(
    store: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = GitHub([_full_history(), [[_attempt(3, NOW + 100)]]])
    monkeypatch.setattr(CONTROLLER, "gh_json", github)
    first = _job(monkeypatch, tmp_path, "job-1", NOW)
    second = _job(monkeypatch, tmp_path, "job-2", NOW + 200)
    assert github.pages == [12, 1]
    assert github.since[1] == (NOW - 1) - (NOW - 1) % 60
    assert len(first) == 2
    assert [attempt.epoch for attempt in second] == [NOW - 10, NOW - 9, NOW + 100]


def test_the_store_row_keeps_only_trusted_markers(
    store: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged = {**_attempt(9, NOW - 5), "performed_via_github_app": {"slug": "worker-app"}}
    history = _full_history()
    history[1][0] = forged
    monkeypatch.setattr(CONTROLLER, "gh_json", GitHub([history]))
    _job(monkeypatch, tmp_path, "job-1", NOW)
    found = cursors.get(CONTROLLER.REPO, "attempt-history")
    assert found is not None
    cursor_at, detail = found
    assert cursor_at == NOW
    assert [row["id"] for row in detail["comments"]] == [1, 2]
    assert all(row["body"].startswith("<!--") for row in detail["comments"])
    assert len(json.dumps(detail)) < 1500


def test_an_edit_that_removes_a_marker_removes_the_attempt(
    store: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    withdrawn = {
        "id": 2,
        "author_association": "OWNER",
        "body": "withdrawn",
        "updated_at": _stamp(NOW + 50),
    }
    monkeypatch.setattr(CONTROLLER, "gh_json", GitHub([_full_history(), [[withdrawn]]]))
    _job(monkeypatch, tmp_path, "job-1", NOW)
    second = _job(monkeypatch, tmp_path, "job-2", NOW + 100)
    assert [attempt.epoch for attempt in second] == [NOW - 10]


def test_a_snapshot_older_than_six_hours_is_read_in_full(
    store: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = GitHub([_full_history(), _full_history()])
    monkeypatch.setattr(CONTROLLER, "gh_json", github)
    _job(monkeypatch, tmp_path, "job-1", NOW)
    _job(monkeypatch, tmp_path, "job-2", NOW + 6 * 3600 + 1)
    assert github.pages == [12, 12]


def test_a_store_failure_costs_only_a_full_read(
    store: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(cursors, "get", broken)
    monkeypatch.setattr(cursors, "advance", broken)
    github = GitHub([_full_history(), _full_history()])
    monkeypatch.setattr(CONTROLLER, "gh_json", github)
    _job(monkeypatch, tmp_path, "job-1", NOW)
    assert len(_job(monkeypatch, tmp_path, "job-2", NOW + 100)) == 2
    assert github.pages == [12, 12]


def test_a_cursor_never_moves_backwards(store: object) -> None:
    assert cursors.advance("acme/widgets", "x", 200, {"v": 2})
    assert not cursors.advance("acme/widgets", "x", 100, {"v": 1})
    assert cursors.get("acme/widgets", "x") == (200, {"v": 2})


def test_one_caller_claims_each_interval(store: object) -> None:
    assert cursors.claim_interval("acme/widgets", "sweep", 3600)
    assert not cursors.claim_interval("acme/widgets", "sweep", 3600)
    assert cursors.claim_interval("acme/widgets", "sweep", 0)
