"""GitHub API quota and truthful retry information in the factory CLI."""

from __future__ import annotations

import argparse
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from rotisserie.factory_console import commands
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubRateLimitError,
    _classify,
)
from rotisserie.factory_console.tracker import Clock

NOW = datetime(2026, 11, 23, 13, 40, tzinfo=UTC)
RESET = int(NOW.timestamp()) + 1500
QUOTA = {
    "resources": {
        "core": {"limit": 5000, "remaining": 0, "used": 5000, "reset": RESET},
        "graphql": {"limit": 5000, "remaining": 4800, "used": 200, "reset": RESET},
        "search": {"limit": 30, "remaining": 25, "used": 5, "reset": RESET},
    }
}


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    command: Sequence[str],
    *,
    blocked: GitHubRateLimitError | None = None,
    quota: object = QUOTA,
) -> tuple[int, str, list[str]]:
    """Use real argument parsing and the CLI with a scripted, network-free gh adapter."""
    calls: list[str] = []

    def github_run(
        self: GitHub,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        allow_exit_one: bool = False,
    ) -> str:
        assert args[0] == "api"
        path = args[1]
        calls.append(path)
        if path == "rate_limit":
            if isinstance(quota, Exception):
                raise quota
            return json.dumps(quota)
        if blocked is not None:
            raise blocked
        return json.dumps({})

    monkeypatch.setattr(GitHub, "_run", github_run)
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    commands.add_parser(subs)
    arguments = parser.parse_args(["factory", *command])
    stdout = io.StringIO()
    code = commands.main(
        arguments,
        env={"FACTORY_REPO": "acme/widgets"},
        stdout=stdout,
        clock=Clock(now=lambda: NOW),
    )
    return code, stdout.getvalue(), calls


def test_limits_command_shows_real_quota_buckets_and_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, calls = invoke(monkeypatch, ["limits"])
    assert code == 0
    assert calls == ["rate_limit"]
    assert "core: 0 / 5,000 remaining (5,000 used)" in out
    assert "graphql: 4,800 / 5,000 remaining" in out
    assert "search: 25 / 30 remaining" in out
    assert "in 25m 00s" in out


def test_limits_json_includes_resource_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, _ = invoke(monkeypatch, ["limits", "--json"])
    assert code == 0
    result = json.loads(out)
    assert result["rate_limits"]["core"]["remaining"] == 0
    assert result["rate_limits"]["core"]["reset_at"].startswith("2026-11-23T")
    assert result["rate_limits"]["graphql"]["used"] == 200


def test_doctor_rate_limit_stops_probing_and_reports_when_it_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, calls = invoke(
        monkeypatch,
        ["doctor", "--fix"],
        blocked=GitHubRateLimitError("GitHub's API rate limit is exhausted."),
    )
    assert code == 4
    assert len(calls) == 2
    assert calls[0].startswith("repos/acme/widgets/contents/factory-stats.json")
    assert calls[1] == "rate_limit"
    assert "GitHub API: GitHub's API rate limit is exhausted" in out
    assert "9 checks not run because GitHub is rate limited" in out
    assert "Earliest known retry:" in out
    assert "in 25m 00s" in out
    assert "Repairs deferred" in out
    assert "Nothing to repair automatically" not in out


def test_doctor_json_describes_deferred_work_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, _ = invoke(
        monkeypatch,
        ["doctor", "--fix", "--json"],
        blocked=GitHubRateLimitError("GitHub's API rate limit is exhausted."),
    )
    result = json.loads(out)
    assert code == 4
    assert result["rate_limit_block"]["kind"] == "primary_or_unknown"
    assert result["rate_limit_block"]["retry_at"] == datetime.fromtimestamp(RESET, UTC).isoformat()
    assert result["applied"] == []
    assert any(f["check"] == "checks skipped" for f in result["findings"])


def test_secondary_throttle_never_fakes_primary_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, _ = invoke(
        monkeypatch,
        ["doctor"],
        blocked=GitHubRateLimitError("secondary rate limit", secondary=True),
    )
    assert code == 4
    assert "Retry time unknown:" in out
    assert "Earliest known retry:" not in out


def test_retry_after_is_used_in_preference_to_bucket_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, _ = invoke(
        monkeypatch,
        ["doctor"],
        blocked=GitHubRateLimitError(
            "secondary rate limit", secondary=True, retry_after_seconds=90
        ),
    )
    assert code == 4
    assert "in 1m 30s" in out
    assert "GitHub Retry-After" in out


def test_missing_quota_endpoint_keeps_retry_time_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, calls = invoke(
        monkeypatch,
        ["doctor"],
        blocked=GitHubRateLimitError("primary limit exceeded"),
        quota=GitHubError("quota endpoint unavailable"),
    )
    assert code == 4
    assert calls[-1] == "rate_limit"
    assert "Cannot read GitHub API quota" in out
    assert "Retry time unknown:" in out


def test_rate_limit_classification_keeps_secondary_retry_after() -> None:
    error = _classify("gh: You have exceeded a secondary rate limit. Retry-After: 90 (HTTP 403)")
    assert isinstance(error, GitHubRateLimitError)
    assert error.secondary is True
    assert error.retry_after_seconds == 90


def test_other_command_failure_also_reports_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, calls = invoke(
        monkeypatch,
        ["status"],
        blocked=GitHubRateLimitError("GitHub's API rate limit is exhausted."),
    )
    assert code == 5
    assert calls[-1] == "rate_limit"
    assert "Earliest known retry:" in out
