"""A fresh capacity cooldown never inherits a longer end from a different cause.

The 2026-11-25 ComicPile case: hungerg-free had a flat 6 hour model_unavailable
health row until 15:55Z. Run 45623514067 hit a real throttle at 13:28Z, and the
forced run 45626172754 then logged "provider_throttle until 2026-11-25 15:55 UTC"
instead of the 5 minute throttle default.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import factory_forced_model as forced  # noqa: E402
import factory_quota_reset as reset  # noqa: E402
import factory_state_operations as operations  # noqa: E402

from tests import turso_support as support  # noqa: E402

MODEL = "vendor/hot"
REQUESTED = "nvidia/" + MODEL
SIX_HOURS = 6 * 3600
SLACK = 30


@pytest.fixture
def store(tmp_path, monkeypatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        support.use_roster(monkeypatch, {"7": ("nvidia", "")})
        yield db, repo


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _old_row(db, repo, outcome: str, status: str, age: int, length: int) -> int:
    written = _now() - age
    db.batch(
        [
            (
                "INSERT INTO factory_model_health (repo, source, model, status, last_outcome,"
                " cooldown_until, last_run_id, updated_at) VALUES (:repo, 'nvidia', :model,"
                " :status, :outcome, :until, 1, :written)",
                {
                    "repo": repo,
                    "model": MODEL,
                    "status": status,
                    "outcome": outcome,
                    "until": written + length,
                    "written": written,
                },
            )
        ]
    )
    return written


def _kept(db, repo) -> None:
    db.batch(
        [
            (
                "INSERT INTO factory_state_events (repo, origin, event_type, subject_type,"
                " source, model, created_at) VALUES (:repo, 'test', 'worker_completed',"
                " 'worker', 'nvidia', :model, unixepoch() - 3600)",
                {"repo": repo, "model": MODEL},
            )
        ]
    )


def _refusal(repo, run_id: int, outcome: str, **extra) -> None:
    assert operations.record_runtime(
        repo,
        run_id,
        {
            "worker": "7",
            "source": "nvidia",
            "model": MODEL,
            "outcome_class": outcome,
            "exact_model_proven": False,
            "persisted_work": False,
            "configured": True,
            **extra,
        },
    )


def _health(db, repo) -> dict:
    (row,) = db.read(
        "SELECT * FROM factory_model_health WHERE repo = :r AND model = :m",
        {"r": repo, "m": MODEL},
    )
    return row


def test_a_throttle_after_an_old_unavailable_row_ends_at_the_throttle_default(store):
    db, repo = store
    _kept(db, repo)
    _old_row(db, repo, "model_unavailable", "unavailable", int(3.5 * 3600), SIX_HOURS)
    _refusal(repo, 45623514067, "provider_throttle")
    row = _health(db, repo)
    expected = _now() + reset.THROTTLE_DEFAULT_SECONDS
    assert row["last_outcome"] == "provider_throttle"
    assert abs(row["cooldown_until"] - expected) <= SLACK
    _, note = forced.forced_exclusions(repo, "7", REQUESTED)
    assert note.startswith("forced run kept vendor/hot excluded: provider_throttle until")
    shown = datetime.strptime(note.rsplit("until ", 1)[1], "%Y-%m-%d %H:%M UTC")
    assert abs(shown.replace(tzinfo=UTC).timestamp() - expected) <= 90


def test_a_recent_unavailable_row_keeps_only_its_current_policy_end(store):
    db, repo = store
    _kept(db, repo)
    written = _old_row(db, repo, "model_unavailable", "unavailable", 120, SIX_HOURS)
    _refusal(repo, 45623514067, "provider_throttle")
    row = _health(db, repo)
    assert row["last_outcome"] == "model_unavailable" and row["status"] == "unavailable"
    assert row["cooldown_until"] == written + 900
    listed = operations.export_backoffs(repo)["providers"]["nvidia"]["health"]
    assert [(h["model"], h["outcome"]) for h in listed] == [(MODEL, "model_unavailable")]


def test_a_real_longer_quota_cooldown_is_kept_and_named(store):
    db, repo = store
    quota = _now() + SIX_HOURS
    hint = {
        "source": "nvidia",
        "model": MODEL,
        "outcome": "provider_quota",
        "kind": "daily",
        "reset_at": quota,
        "reset_source": "header",
    }
    _refusal(repo, 11, "provider_quota", quota_hints=[hint])
    _refusal(repo, 12, "provider_throttle")
    row = _health(db, repo)
    assert row["cooldown_until"] == quota
    assert row["last_outcome"] == "provider_quota"
    assert MODEL in operations.export_model_exclusions(repo, "7", "")["nvidia"]


def test_readers_clamp_an_own_failure_row_to_its_cap(store):
    db, repo = store
    _old_row(db, repo, "provider_failure", "cooling", 3 * 3600, 24 * 3600)
    assert MODEL not in operations.export_model_exclusions(repo, "7", "").get("nvidia", [])
    listed = operations.export_backoffs(repo)["providers"].get("nvidia", {})
    assert not listed.get("health")
