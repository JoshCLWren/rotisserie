"""Focused tests: no GitHub API calls or credentials needed."""

from datetime import UTC, datetime
from typing import Any

import pytest

from scripts.sync_factory_quota import aggregate


def observation(time: str, remaining: int) -> dict[str, Any]:
    return {
        "observed_at": time,
        "credential_kind": "github-actions-installation",
        "public_alias": "factory-comicpile-actions",
        "resources": {
            "core": {
                "limit": 5000,
                "remaining": remaining,
                "used": 5000 - remaining,
                "reset_at": "2026-10-10T18:00:00Z",
                "untrusted_private_path": "secret",
            }
        },
    }


def test_history_low_water_and_single_exhaustion_transition() -> None:
    now = datetime(2026, 10, 10, 17, 0, tzinfo=UTC)
    data = aggregate(
        [
            observation("2026-10-10T14:00:00Z", 70),
            observation("2026-10-10T15:00:00Z", 0),
            observation("2026-10-10T15:30:00Z", 0),
            observation("2026-10-10T16:00:00Z", 120),
        ],
        now,
    )
    core = data["installations"][0]["core"]
    assert core["low_water_24h"] == 0
    assert core["remaining"] == 120
    assert data["exhaustion_incidents_24h"] == 1
    assert data["top_consumers"] is None
    assert "secret" not in str(data)


def test_no_measurements_cannot_publish_fake_healthy_data() -> None:
    with pytest.raises(ValueError, match="No authenticated"):
        aggregate([], datetime(2026, 10, 10, 17, 0, tzinfo=UTC))


def test_rejects_old_samples_even_if_publisher_runs_today() -> None:
    with pytest.raises(ValueError, match="stale"):
        aggregate(
            [observation("2026-10-10T10:00:00Z", 4990)],
            datetime(2026, 10, 10, 17, 0, tzinfo=UTC),
        )


def test_invalid_bucket_cannot_publish() -> None:
    bad = observation("2026-10-10T16:00:00Z", 4500)
    bad["resources"]["core"]["limit"] = -1
    with pytest.raises(ValueError, match="No validated"):
        aggregate([bad], datetime(2026, 10, 10, 17, 0, tzinfo=UTC))
