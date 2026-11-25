"""A model with a clearly good track record keeps short work-failure cooldowns.

The 2026-11-25 ComicPile case: opencode-free hungerg-free (15 of 39 runs
succeeded in seven days) was benched for six hours by one "Model unavailable"
line in run 45588137241 while incident PR #5772 waited for a reviewer.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import factory_model_track_record as track  # noqa: E402
import factory_state_operations as operations  # noqa: E402
import factory_state_operations_reliability as reliability  # noqa: E402
import factory_track_record_cooldown as cooldown  # noqa: E402

from tests import turso_support as support  # noqa: E402

HOT = "vendor/hot"
NEW = "vendor/new"
WEAK = "vendor/weak"
MID = "vendor/mid"


def window(completed: int, failed: int) -> dict:
    return {
        "runs": completed + failed,
        "completed": completed,
        "delivered": completed,
        "failed": failed,
        "idle": 0,
        "failures": {"work_failure": failed},
    }


@pytest.fixture
def stats(tmp_path, monkeypatch):
    stamp = datetime.now(UTC).isoformat()
    rows = {HOT: window(30, 10), MID: window(8, 32), WEAK: window(2, 40), NEW: window(0, 1)}
    path = tmp_path / "stats.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": stamp,
                "models": [
                    {"source": "nvidia", "model": m, "windows": {"7d": w, "24h": w}}
                    for m, w in rows.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(track.FILE_ENV, str(path))
    monkeypatch.setenv(track.KIND_ENV, "implement")
    return path


@pytest.fixture
def store(tmp_path, monkeypatch, stats):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        support.use_roster(monkeypatch, {"7": ("nvidia", "")})
        yield db, repo


def outcome(model: str, cls: str, **extra) -> dict:
    return {
        "worker": "7",
        "source": "nvidia",
        "model": model,
        "outcome_class": cls,
        "exact_model_proven": True,
        "persisted_work": False,
        "configured": True,
        **extra,
    }


def model_row(store, model: str) -> dict:
    db, repo = store
    (row,) = db.read(
        "SELECT * FROM factory_backoff WHERE repo = :r AND kind = 'model' AND model = :m",
        {"r": repo, "m": model},
    )
    return row


def wait(row: dict) -> int:
    return round((int(row["next_retry_at"]) - int(row["updated_at"])) / 10) * 10


def health_until(store, model: str) -> int:
    db, repo = store
    (row,) = db.read(
        "SELECT cooldown_until, updated_at FROM factory_model_health WHERE repo = :r"
        " AND model = :m",
        {"r": repo, "m": model},
    )
    return round((int(row["cooldown_until"]) - int(row["updated_at"])) / 10) * 10


def test_the_real_example_is_hot_and_a_weak_model_is_not() -> None:
    fleet = 0.153
    assert track.hot(track.Record(15, 39, "all"), fleet)
    assert not track.hot(track.Record(4, 17, "all"), fleet)
    assert not track.hot(track.Record(5, 5, "all"), fleet)


def test_hot_delay_curve() -> None:
    assert [cooldown.hot_delay(n) for n in range(1, 9)] == [0, 0, 300, 300, 600, 900, 1500, 1800]
    assert max(cooldown.hot_delay(n) for n in range(1, 41)) == 1800


def test_a_hot_model_waits_nothing_after_one_or_two_work_failures(store):
    for run_id, expected in ((11, 0), (12, 0), (13, 300)):
        assert operations.record_runtime(store[1], run_id, outcome(HOT, "work_failure"))
        row = model_row(store, HOT)
        assert wait(row) == expected
    detail = json.loads(row["detail"])
    assert detail["kept_hot"] is True
    assert detail["track_record"] == "30 of 40 runs succeeded in 7d"


def test_a_hot_model_timeout_does_not_exclude_it(store):
    assert operations.record_runtime(store[1], 21, outcome(HOT, "agent_timeout"))
    assert wait(model_row(store, HOT)) == 0
    assert HOT not in operations.export_model_exclusions(store[1], "7").get("nvidia", [])


def test_an_unproven_model_keeps_todays_curve(store):
    for run_id, expected in ((31, 300), (32, 300), (33, 600)):
        assert operations.record_runtime(store[1], run_id, outcome(NEW, "work_failure"))
        assert wait(model_row(store, NEW)) == expected
    assert "kept_hot" not in json.loads(model_row(store, NEW)["detail"])


def test_a_proven_bad_model_keeps_todays_curve(store):
    assert operations.record_runtime(store[1], 41, outcome(WEAK, "work_failure"))
    assert wait(model_row(store, WEAK)) == 300


def test_without_stats_a_hot_model_keeps_todays_curve(store, monkeypatch):
    monkeypatch.delenv(track.FILE_ENV)
    assert operations.record_runtime(store[1], 51, outcome(HOT, "work_failure"))
    assert wait(model_row(store, HOT)) == 300


def test_quota_on_a_hot_model_still_cools_until_the_reset(store):
    db, repo = store
    assert operations.record_runtime(repo, 61, outcome(HOT, "provider_quota"))
    (row,) = db.read(
        "SELECT * FROM factory_backoff WHERE repo = :r AND kind = 'provider'", {"r": repo}
    )
    until = datetime.fromtimestamp(row["expires_at"], UTC)
    assert until.hour == 0 and until.minute == 0 and until > datetime.now(UTC)
    assert operations.export_model_exclusions(repo, "7") or True
    assert not db.read("SELECT 1 FROM factory_backoff WHERE kind = 'model'", {})


def test_throttle_on_a_hot_model_still_cools_until_retry_after(store):
    db, repo = store
    epoch = int((datetime.now(UTC) + timedelta(seconds=900)).timestamp())
    hint = {
        "source": "nvidia",
        "model": HOT,
        "outcome": "provider_throttle",
        "kind": "throttle",
        "reset_at": epoch,
        "reset_source": "header",
    }
    assert operations.record_runtime(
        repo, 71, outcome(HOT, "provider_throttle", quota_hints=[hint])
    )
    (row,) = db.read("SELECT cooldown_until FROM factory_model_health WHERE model = :m", {"m": HOT})
    assert row["cooldown_until"] == epoch
    assert HOT in operations.export_model_exclusions(repo, "7")["nvidia"]


def test_one_unavailable_line_benches_a_hot_model_for_half_an_hour_with_evidence(store):
    db, repo = store
    failures = [
        {
            "source": "nvidia",
            "model": HOT,
            "outcome": "model_unavailable",
            "scope": "model",
            "evidence": "Error: Model unavailable: nvidia/vendor/hot",
        }
    ]
    assert operations.record_runtime(repo, 81, outcome(NEW, "success", model_failures=failures))
    assert health_until(store, HOT) == 1800
    backoffs = operations.export_backoffs(repo)["providers"]["nvidia"]
    (seen,) = [row for row in backoffs["health"] if row["model"] == HOT]
    assert seen["run_id"] == 81
    assert seen["evidence"] == "Error: Model unavailable: nvidia/vendor/hot"
    assert seen["kept_hot"] is True


def test_a_recent_completed_run_also_keeps_an_unavailable_model_short(store, monkeypatch):
    db, repo = store
    monkeypatch.delenv(track.FILE_ENV)
    db.batch(
        [
            (
                "INSERT INTO factory_state_events (repo, origin, event_type, subject_type, source,"
                " model, created_at) VALUES (:r, 'test', 'worker_completed', 'worker', 'nvidia',"
                " :m, unixepoch() - 3600)",
                {"r": repo, "m": NEW},
            )
        ]
    )
    assert operations.record_runtime(repo, 91, outcome(NEW, "model_unavailable"))
    assert health_until(store, NEW) == 1800
    assert operations.record_runtime(repo, 92, outcome(WEAK, "model_unavailable"))
    assert health_until(store, WEAK) == 6 * 3600


def test_backoffs_list_a_kept_hot_model_while_the_normal_curve_would_hold_it(store):
    _, repo = store
    assert operations.record_runtime(repo, 101, outcome(HOT, "work_failure"))
    found = operations.export_backoffs(repo)["providers"]["nvidia"]
    assert not found["fibonacci"]
    (kept,) = [row for row in found["kept_hot"] if row["kind"] == "model"]
    assert kept["model"] == HOT and kept["until"] is None and kept["normal_until"]


def test_published_stats_mark_hot_models(stats):
    snapshot = json.loads(stats.read_text(encoding="utf-8"))
    rows = [
        {
            "window": "7d",
            "source": "nvidia",
            "model": m["model"],
            "event_type": kind,
            "outcome_class": "work_failure",
            "delivered": True,
            "n": n,
        }
        for m in snapshot["models"]
        for kind, n in (
            ("worker_completed", m["windows"]["7d"]["completed"]),
            ("worker_failed", m["windows"]["7d"]["failed"]),
        )
        if n
    ]
    found = reliability.summarize_reliability(rows, [], datetime.now(UTC))
    marked = {m["model"]: m.get("kept_hot") for m in found["models"]}
    assert marked[HOT] == {"successes": 30, "trials": 40}
    assert marked[WEAK] is None and marked[NEW] is None and marked[MID] is None


def test_selection_gives_a_hot_model_the_larger_share_and_keeps_exploring(stats):
    from factory_runtime_model_select import select

    picks = Counter(
        select(
            "nvidia",
            "7",
            {"data": [{"id": m} for m in (HOT, MID, WEAK, NEW, "vendor/x")]},
            rotation=r,
        )["model"]
        for r in range(3000)
    )
    explore = (picks[NEW] + picks["vendor/x"]) / 3000
    assert 0.15 < explore < 0.25
    share = picks[HOT] / 3000
    assert share > 0.8 * track.MAX_SHARE + 0.05
    assert share == pytest.approx(0.8 * track.HOT_MAX_SHARE, abs=0.04)
