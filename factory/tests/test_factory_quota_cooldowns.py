"""Quota and throttle refusals cool until the provider's reset, never as a streak."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import factory_quota_probe as probe  # noqa: E402
import factory_quota_reset as reset  # noqa: E402
import factory_state_operations as operations  # noqa: E402

from tests import turso_support as support  # noqa: E402

OBSERVED = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)
MIDNIGHT = datetime(2026, 11, 25, tzinfo=UTC)


@pytest.fixture
def store(tmp_path, monkeypatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        support.use_roster(monkeypatch, {"7": ("nvidia", "")})
        yield db, repo


def outcome(run_id: int, cls: str, **extra) -> dict:
    return {
        "worker": "7",
        "source": "nvidia",
        "model": "vendor/m",
        "outcome_class": cls,
        "exact_model_proven": False,
        "persisted_work": False,
        "configured": True,
        **extra,
    }


def project(store, run_id, out):
    _, repo = store
    return operations.record_runtime(repo, run_id, out)


def backoffs(store, kind: str) -> list[dict]:
    db, repo = store
    return db.read(
        "SELECT * FROM factory_backoff WHERE repo = :r AND kind = :k", {"r": repo, "k": kind}
    )


def health(store) -> list[tuple[str, str]]:
    db, repo = store
    rows = db.read(
        "SELECT model, last_outcome FROM factory_model_health WHERE repo = :r ORDER BY model",
        {"r": repo},
    )
    return [(r["model"], r["last_outcome"]) for r in rows]


def test_quota_never_grows_a_streak_and_cools_the_account_until_reset(store):
    for run_id in (11, 12, 13):
        assert project(store, run_id, outcome(run_id, "provider_quota"))
    assert not backoffs(store, "model")
    assert not backoffs(store, "worker")
    (row,) = backoffs(store, "provider")
    assert row["outcome"] == "provider_quota" and row["streak"] == 0
    assert json.loads(row["detail"])["reset_source"] == "policy"
    until = datetime.fromtimestamp(row["expires_at"], UTC)
    assert until.hour == 0 and until.minute == 0 and until > datetime.now(UTC)


def test_throttle_cools_only_the_refused_model(store):
    epoch = int((datetime.now(UTC) + timedelta(seconds=90)).timestamp())
    hint = {
        "source": "nvidia",
        "model": "vendor/m",
        "outcome": "provider_throttle",
        "kind": "throttle",
        "reset_at": epoch,
        "reset_source": "header",
    }
    assert project(store, 21, outcome(21, "provider_throttle", quota_hints=[hint]))
    assert not backoffs(store, "provider")
    assert not backoffs(store, "model")
    assert health(store) == [("vendor/m", "provider_throttle")]


def test_real_failure_still_grows_a_capped_streak(store):
    assert project(store, 31, outcome(31, "agent_timeout", exact_model_proven=True))
    (row,) = backoffs(store, "model")
    assert row["streak"] == 1
    assert operations.fibonacci_worker_delay(40) == 86400
    assert operations.fibonacci_retry_at(OBSERVED, 40) == OBSERVED + timedelta(days=1)


def test_pool_refusal_cools_priced_models_only(store):
    failures = [
        {"source": "nvidia", "model": "vendor/paid", "outcome": "provider_quota", "scope": "pool"}
    ]
    assert project(
        store, 41, outcome(41, "success", exact_model_proven=True, model_failures=failures)
    )
    assert backoffs(store, "pool")
    assert not backoffs(store, "provider")
    assert [m for m, o in health(store) if o == "provider_quota"] == ["vendor/paid"]


def test_oversized_request_sets_no_cooldown(store):
    hint = {
        "source": "nvidia",
        "model": "vendor/m",
        "outcome": "provider_quota",
        "kind": "oversize",
        "reset_at": 0,
        "reset_source": "text",
    }
    assert project(store, 51, outcome(51, "provider_quota", quota_hints=[hint]))
    assert not backoffs(store, "provider") and not backoffs(store, "model")


@pytest.mark.parametrize(
    "observed,expected",
    [
        ("2026-11-24T15:30:00+00:00", "2026-11-25T00:00:00+00:00"),
        ("2026-12-31T23:59:59+00:00", "2027-01-01T00:00:00+00:00"),
    ],
)
def test_openrouter_quota_resets_at_the_next_utc_midnight(observed, expected):
    at = datetime.fromisoformat(observed)
    assert reset.resolve(
        "openrouter-free", "provider_quota", None, at
    ).until == datetime.fromisoformat(expected)
    assert reset.scope("openrouter-free", "daily") == "account"


def test_refusal_texts_map_to_kinds_and_waits():
    groq = (
        "Rate limit reached for model `openai/gpt-oss-120b` on requests per day (RPD): "
        "Limit 1000, Used 1000, Requested 1. Please try again in 1m26.4s."
    )
    found = reset.from_text("groq-free", "provider_quota", groq, OBSERVED)
    assert (found.kind, found.reset_source) == ("daily", "text")
    assert found.until == OBSERVED + timedelta(seconds=86.4)
    itpm = "Limit reached on tokens per minute (ITPM): Limit 7000, Requested 23407"
    assert reset.from_text("groq-free", "provider_quota", itpm, OBSERVED).kind == "oversize"
    for text in (
        "Insufficient balance or no resource package. Please recharge.",
        "Add usage credits or subscribe and save up to 66%",
    ):
        found = reset.from_text("z-ai", "provider_quota", text, OBSERVED)
        assert found.kind == "needs_credits" and reset.scope("z-ai", found.kind) == "pool"
    nvidia = 'Provider request failed with HTTP 429: {"status":429,"title":"Too Many Requests"}'
    found = reset.from_text("nvidia", "provider_throttle", nvidia, OBSERVED)
    assert found.until == OBSERVED + timedelta(seconds=reset.THROTTLE_DEFAULT_SECONDS)
    openrouter = "Rate limit exceeded: free-models-per-day-high-balance"
    assert (
        reset.from_text("openrouter-free", "provider_throttle", openrouter, OBSERVED).until
        == MIDNIGHT
    )


def test_marker_round_trip_through_the_trusted_log():
    from factory_state_ledger_events import quota_reset_hints

    line = (
        "2026-11-24T15:30:00.0000000Z [factory:7][source:nvidia][model:vendor/m] "
        "FACTORY_QUOTA_RESET: source=nvidia model=vendor/m outcome=provider_throttle "
        "kind=throttle reset_at=1764000000 reset_source=header\n"
    )
    forged = line.replace("[factory:7]", "[factory:8]")
    assert quota_reset_hints(line + forged, "7") == [
        {
            "source": "nvidia",
            "model": "vendor/m",
            "outcome": "provider_throttle",
            "kind": "throttle",
            "reset_at": 1764000000,
            "reset_source": "header",
        }
    ]


def test_legacy_quota_fibonacci_rows_no_longer_gate_and_old_rows_are_capped():
    from factory_state_operations_backoff import effective_retry_at, worker_backoff_reason

    far = (OBSERVED + timedelta(days=7)).isoformat()
    quota = {
        "payload": [
            {"source": "nvidia", "model": "m", "outcome": "provider_quota", "next_retry_at": far}
        ],
        "expires_at": OBSERVED,
    }
    assert worker_backoff_reason(quota, "nvidia", "m", OBSERVED) is None
    real = {"source": "nvidia", "model": "m", "outcome": "agent_timeout", "next_retry_at": far}
    assert effective_retry_at(real, OBSERVED) == OBSERVED + timedelta(minutes=30)
    fault = {**real, "outcome": "environment_failure"}
    assert effective_retry_at(fault, OBSERVED) == OBSERVED + timedelta(hours=2)


def test_fleet_failover_and_exclusions_honor_capacity_cooldowns(store, monkeypatch):
    import factory_source_budget

    db, repo = store
    monkeypatch.setattr(factory_source_budget, "load_budgets", lambda: {})
    for kind, source in (("provider", "groq-free"), ("pool", "z-ai")):
        support.backoff(db, repo, kind, source=source, outcome="provider_quota", expires_in=7200)
    sources = operations.export_fleet_failover_sources(repo, "7")
    assert "groq-free" not in sources and "z-ai" in sources and "nvidia" in sources
    assert operations.export_model_exclusions(repo)["@credit_pool"] == ["z-ai"]


def test_failover_skips_priced_models_of_a_credit_cooled_source():
    from factory_model_failover import select_next

    catalog = {
        "data": [
            {"id": "paid", "pricing": {"prompt": "0.1", "completion": "0.2"}},
            {"id": "free", "pricing": {"prompt": "0", "completion": "0"}},
        ]
    }
    picks = {
        select_next(
            "nvidia",
            "7",
            set(),
            {"@credit_pool": ["nvidia"]},
            catalog_json=catalog,
            proven=set(),
            rotation=r,
        )["model"]
        for r in range(12)
    }
    assert picks == {"free"}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def refusal(code: int, headers: dict[str, str]):
    message = Message()
    for name, value in headers.items():
        message[name] = value

    def opener(request):
        raise urllib.error.HTTPError(request.full_url, code, "refused", message, None)

    return opener


ENV = {"NVIDIA_API_KEY": "sk-secret-value-123", "OPENROUTER_API_KEY": "sk-or-secret"}


def test_probe_honors_retry_after(tmp_path):
    found = probe.probe_reset(
        "nvidia",
        "vendor/m",
        "throttle",
        OBSERVED,
        tmp_path / "c.json",
        env=ENV,
        opener=refusal(429, {"Retry-After": "120"}),
    )
    assert found.until == OBSERVED + timedelta(seconds=120) and found.reset_source == "header"


def test_probe_honors_x_ratelimit_reset_in_milliseconds(tmp_path):
    epoch_ms = int((OBSERVED + timedelta(minutes=40)).timestamp() * 1000)
    found = probe.probe_reset(
        "nvidia",
        "vendor/m",
        "daily",
        OBSERVED,
        tmp_path / "c.json",
        env=ENV,
        opener=refusal(429, {"X-RateLimit-Reset": str(epoch_ms)}),
    )
    assert found.until == OBSERVED + timedelta(minutes=40)


@pytest.mark.parametrize(
    "code,headers", [(429, {}), (401, {"Retry-After": "60"}), (403, {"Retry-After": "60"})]
)
def test_probe_without_a_hint_or_blocked_falls_back_to_policy(tmp_path, code, headers):
    assert (
        probe.probe_reset(
            "nvidia",
            "vendor/m",
            "throttle",
            OBSERVED,
            tmp_path / "c.json",
            env=ENV,
            opener=refusal(code, headers),
        )
        is None
    )


def test_probe_runs_once_per_source_per_window(tmp_path):
    calls = []

    def opener(request):
        calls.append(request.full_url)
        return refusal(429, {"Retry-After": "300"})(request)

    cache = tmp_path / "c.json"
    first = probe.probe_reset("nvidia", "a", "throttle", OBSERVED, cache, env=ENV, opener=opener)
    second = probe.probe_reset("nvidia", "b", "throttle", OBSERVED, cache, env=ENV, opener=opener)
    assert len(calls) == 1 and first.until == second.until


def test_probe_never_runs_without_known_request_headers(tmp_path):
    def opener(request):
        raise AssertionError("opencode-free must not be probed")

    assert (
        probe.probe_reset(
            "opencode-free", "m", "throttle", OBSERVED, tmp_path / "c.json", env=ENV, opener=opener
        )
        is None
    )


def test_openrouter_key_endpoint_with_no_remaining_requests(tmp_path):
    body = {"data": {"free_model_daily_requests": {"used": 1000, "limit": 1000, "remaining": 0}}}
    seen = []

    def opener(request):
        seen.append((request.full_url, request.get_method()))
        return Response(json.dumps(body).encode())

    found = probe.probe_reset(
        "openrouter-free", "x:free", "daily", OBSERVED, tmp_path / "c.json", env=ENV, opener=opener
    )
    assert seen == [(probe.OPENROUTER_KEY_URL, "GET")]
    assert (found.until, found.reset_source) == (MIDNIGHT, "key_endpoint")


def test_hint_cli_never_prints_the_key(tmp_path, monkeypatch, capsys):
    log = tmp_path / "opencode.log"
    log.write_text("Provider request failed with HTTP 429 Too Many Requests\n")
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-secret-value-123")
    monkeypatch.setattr(probe, "_open", refusal(429, {"Retry-After": "45"}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "factory_quota_reset.py",
            "hint",
            "--source",
            "nvidia",
            "--model",
            "vendor/m",
            "--outcome",
            "provider_throttle",
            "--log",
            str(log),
            "--probe",
            "--probe-cache",
            str(tmp_path / "c.json"),
        ],
    )
    assert reset.main() == 0
    out = capsys.readouterr()
    assert "sk-secret" not in out.out + out.err
    assert reset.parse_marker("FACTORY_QUOTA_RESET: " + out.out.strip())["reset_source"] == "header"
    assert "sk-secret" not in (tmp_path / "c.json").read_text()


def test_probe_sends_the_headers_opencode_adds_for_nvidia(tmp_path):
    seen = []

    def opener(request):
        seen.append(dict(request.header_items()))
        return refusal(429, {"Retry-After": "30"})(request)

    probe.probe_reset(
        "nvidia", "vendor/m", "throttle", OBSERVED, tmp_path / "c.json", env=ENV, opener=opener
    )
    assert seen[0]["Http-referer"] == "https://opencode.ai/"
    assert seen[0]["X-title"] == "opencode"


def test_agent_prose_never_becomes_a_reset_hint():
    log = (
        "I will wait: try again in 9h and the account has insufficient balance\n"
        '{"type":"text","text":"Please recharge"}\n'
        '{"type":"error","error":{"message":"HTTP 429 Too Many Requests"}}\n'
        "Error: Rate limit exceeded: free-models-per-day-high-balance\n"
    )
    assert reset.refusal_evidence(log) == (
        "HTTP 429 Too Many Requests\nError: Rate limit exceeded: free-models-per-day-high-balance"
    )


def test_no_capacity_is_never_a_worker_failure(store):
    db, repo = store
    assert project(store, 31, outcome(31, "no_capacity"))
    rows = db.read("SELECT failures FROM factory_worker_metrics WHERE repo = :r", {"r": repo})
    assert rows == [{"failures": 0}]
    assert not backoffs(store, "worker")
