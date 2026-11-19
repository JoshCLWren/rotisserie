"""Tests for durable provider-quota pauses of fixed-model workers."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, UTC
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "factory_quota_pause", SCRIPTS / "factory_quota_pause.py"
)
assert _SPEC and _SPEC.loader
qp = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = qp
_SPEC.loader.exec_module(qp)

BEFORE_RESET = datetime(2026, 10, 18, 13, 0, tzinfo=UTC)
AFTER_RESET = datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC)


def _write(tmp_path: Path, pauses: list[dict[str, object]]) -> Path:
    path = tmp_path / "pauses.json"
    path.write_text(json.dumps({"schema_version": 1, "pauses": pauses}), encoding="utf-8")
    return path


def test_repo_config_pauses_w48_and_w55_until_november() -> None:
    """The committed config quota-pauses Mistral w48 and Ollama w55 until ~Nov 1."""
    active = qp.active_pauses(BEFORE_RESET)
    assert set(active) == {48, 55}
    for worker in (48, 55):
        assert active[worker].paused_until == datetime(2026, 11, 1, tzinfo=UTC)
        assert "provider_quota" in active[worker].reason
    assert qp.active_pauses(AFTER_RESET) == {}


def test_retired_workers_stay_unscheduled_after_legacy_pause_expires() -> None:
    """Old quota records cannot reactivate workers that are no longer scheduled."""
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    try:
        slots_mod = importlib.import_module("factory_worker_slots")
    finally:
        if str(SCRIPTS) in sys.path:
            sys.path.remove(str(SCRIPTS))
    active = {slot.worker for slot in slots_mod.slots()}
    for worker in (48, 55):
        assert str(worker) not in active


def test_filter_preserves_order_and_logs_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filtering drops only active pauses and reports why."""
    kept, messages = qp.filter_workers(["21", "48", "46", "55", "60"], now=BEFORE_RESET)
    assert kept == ["21", "46", "60"]
    assert any(
        m.startswith("Factory 48 paused until 2026-11-01T00:00:00Z (config)") for m in messages
    )
    kept, messages = qp.filter_workers(["48", "55"], now=AFTER_RESET)
    assert kept == ["48", "55"] and messages == []


def test_worker_46_can_never_be_paused(tmp_path: Path) -> None:
    """Worker 46 is protected from quota-pause logic."""
    path = _write(
        tmp_path,
        [
            {
                "worker": 46,
                "paused_until": "2026-11-01T00:00:00Z",
                "reason": "x",
                "reset_source": "config",
            }
        ],
    )
    with pytest.raises(ValueError, match="protected"):
        qp.load_pauses(path)


def test_rejects_non_utc_and_duplicates(tmp_path: Path) -> None:
    """Timestamps must be explicit UTC and workers unique."""
    with pytest.raises(ValueError):
        qp.load_pauses(_write(tmp_path, [{"worker": 48, "paused_until": "2026-11-01"}]))
    dup = {"worker": 48, "paused_until": "2026-11-01T00:00:00Z"}
    with pytest.raises(ValueError, match="duplicate"):
        qp.load_pauses(_write(tmp_path, [dup, dup]))


def test_is_paused_handles_non_numeric_ids() -> None:
    """Non-numeric worker ids are never paused."""
    assert qp.is_paused("local") is None
    assert qp.is_paused(None) is None
    assert qp.is_paused("48", now=BEFORE_RESET) is not None


def test_pause_module_parses_on_older_python() -> None:
    """The dispatcher's system python3 may predate 3.14 syntax relaxations."""
    import ast

    source = (SCRIPTS / "factory_quota_pause.py").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 10))


def test_missing_file_means_no_pauses(tmp_path: Path) -> None:
    """A missing pause file disables pausing rather than failing dispatch."""
    assert qp.load_pauses(tmp_path / "absent.json") == []


def test_cli_filter(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The dispatcher CLI emits a compact JSON array on stdout."""
    import io

    monkeypatch.setattr(
        qp,
        "active_pauses",
        lambda now=None, pauses=None: {
            48: qp.QuotaPause(48, "mistral", datetime(2026, 11, 1, tzinfo=UTC), "q", "config")
        },
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO('["48","21"]'))
    assert qp.main(["filter"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == '["21"]'
    assert "Factory 48 paused until" in out.err


def test_classifier_labels_quota_exhaustion() -> None:
    """Quota exhaustion classifies as provider_quota rather than unknown_failure."""
    spec = importlib.util.spec_from_file_location(
        "classify_fixed", SCRIPTS / "classify-fixed-model-run.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    lock = (
        "Factory 48 locked: source=mistral model=codestral-latest runtime=mistral/codestral-latest "
        "minute=:50 scheduler=dispatcher configured=true\n"
    )
    result = module.classify(
        lock + "Smoke exact pinned model through OpenCode\nError: API budget exhausted.\n"
    )
    assert result.outcome_class == "provider_quota"
    assert "provider_quota" in module.CANONICAL_OUTCOME_CLASSES


def test_dispatch_workflows_filter_paused_workers() -> None:
    """Paused workers are excluded before entry capacity/dispatch cap is applied."""
    text = (ROOT / ".github" / "workflows" / "fixed-model-factory-dispatch.yml").read_text(encoding="utf-8")
    assert "factory_quota_pause.py filter" in text
    assert text.index("factory_quota_pause.py filter") < text.index('controller" capacity')


def test_scheduled_batch_filters_pauses_before_offer_cap() -> None:
    """Scheduled batch must not let paused lanes displace eligible workers inside the cap."""
    text = (ROOT / ".github" / "workflows" / "fixed-model-factory-dispatch.yml").read_text(encoding="utf-8")
    assert "factory_quota_pause.py filter" in text
    # Eligibility filtering (including pauses) happens before the dispatch batch cap.
    assert ".[:$cap]" in text or ".[:$max_per_tick]" in text


@pytest.mark.parametrize("source,runtime", [("z-ai", "z-ai/glm-4.5-flash"),
                                              ("kilo-auto", "kilo/kilo-auto/free")])
@pytest.mark.parametrize("message", ["HTTP 429 Too Many Requests", "status code: 429"])
def test_bare_429_is_throttle_in_smoke_log_and_classifier(
    tmp_path: Path, message: str, source: str, runtime: str,
) -> None:
    """Plain 429/rate-limit responses are transient throttles, not quota exhaustion."""
    modules = {}
    sys.path.insert(0, str(SCRIPTS))
    try:
        for name, filename in (
            ("quota_classifier", "classify-fixed-model-run.py"),
            ("quota_smoke", "factory_live_opencode.py"),
        ):
            spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            modules[name] = module
    finally:
        if str(SCRIPTS) in sys.path:
            sys.path.remove(str(SCRIPTS))
    log = (
        f"Factory 97 locked: source={source} model=free runtime={runtime} "
        "minute=:50 scheduler=dispatcher configured=true\n" + message
    )
    assert modules["quota_classifier"].classify(log).outcome_class == "provider_throttle"
    target = tmp_path / 'outcome'
    assert modules["quota_smoke"].record_smoke_failure(message, runtime, target)
    assert target.read_text().startswith('provider_throttle\t')


@pytest.mark.parametrize("source", ["z-ai", "kilo-auto"])
def test_bare_session_429_records_throttle(tmp_path: Path, source: str) -> None:
    """Plain 429 responses produce transient throttle evidence, not quota pause."""
    import subprocess

    script = (SCRIPTS / 'free-model-factory-worker.sh').read_text()
    functions = script[script.index('record_terminal_outcome() {'):script.index(
        '# A PR branch may predate the Kilo integration'
    )]
    worker = f'quota-test-{tmp_path.name}'
    log = Path(f'/tmp/opencode-factory-{worker}.log')
    outcome = tmp_path / 'outcome'
    log.write_text('HTTP 429 Too Many Requests\n')
    try:
        result = subprocess.run(
            ['bash', '-c', f'SOURCE={source}\nWORKER="$1"\nTERMINAL_OUTCOME_FILE="$2"\n'
             + functions + '\nrecord_agent_failure_outcome 1\n', '_', worker, str(outcome)],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert outcome.read_text().startswith('provider_throttle\t')
    finally:
        log.unlink(missing_ok=True)


@pytest.mark.parametrize('observed,reset', [
    ('2026-11-04T04:35:40Z', '2026-12-01T00:00:00Z'),
    ('2026-12-31T23:59:59Z', '2027-01-01T00:00:00Z'),
    ('2028-02-29T12:00:00Z', '2028-03-01T00:00:00Z'),
])
@pytest.mark.parametrize('provider', ['mistral', 'ollama-cloud'])
def test_monthly_quota_uses_next_utc_month(provider: str, observed: str, reset: str) -> None:
    """Calendar fallback handles December and leap years without a fixed day count."""
    timestamp = int(datetime.fromisoformat(observed.replace('Z', '+00:00')).timestamp())
    expected = int(datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp())
    assert qp.quota_reset_epoch(provider, timestamp) == expected
    assert qp.quota_reset_epoch('z-ai', timestamp) == timestamp + 1800


@pytest.mark.parametrize('observed,reset', [
    ('2026-11-14T19:08:00Z', '2026-11-15T00:00:00Z'),
    ('2026-12-31T23:59:59Z', '2027-01-01T00:00:00Z'),
    ('2028-02-29T00:00:00Z', '2028-03-01T00:00:00Z'),
])
def test_openrouter_daily_quota_resets_at_utc_midnight(observed: str, reset: str) -> None:
    observed_epoch = int(datetime.fromisoformat(observed.replace('Z', '+00:00')).timestamp())
    expected_epoch = int(datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp())
    assert qp.quota_reset_epoch('openrouter-free', observed_epoch) == expected_epoch


@pytest.mark.parametrize('runtime,message', [
    ('mistral/codestral-latest', 'Error: API budget exhausted.'),
    ('ollama-cloud/nemotron-3-nano:30b', 'You have reached your monthly usage limit'),
])
def test_monthly_smoke_produces_durable_quota_outcome(
    tmp_path: Path, runtime: str, message: str,
) -> None:
    """Post-reset quota errors must reach the trusted registry, not unknown_failure."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location('monthly_smoke', SCRIPTS / 'factory_live_opencode.py')
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if str(SCRIPTS) in sys.path:
            sys.path.remove(str(SCRIPTS))
    target = tmp_path / 'outcome'
    assert module.record_smoke_failure(message, runtime, target)
    assert target.read_text().startswith('provider_quota\t')


@pytest.mark.parametrize("source", ["z-ai", "kilo-auto"])
def test_free_provider_commit_sha_429_cannot_cause_quota_pause(source: str) -> None:
    """Actual Z-AI history: commit 9cd7bc429 is not an HTTP 429 response."""
    spec = importlib.util.spec_from_file_location(
        f"classify_commit_hash_{source.replace('-', '_')}",
        SCRIPTS / "classify-fixed-model-run.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    log = (
        f"Factory 54 locked: source={source} model=free runtime={source}/free "
        "minute=:50 scheduler=dispatcher configured=true\n"
        "FIXED_MODEL_OPENCODE_OK\n"
        "2026-11-14T16:45:56.5705392Z 9cd7bc429 improve workflow\n"
        "[factory:54] repair attempt for PR #5060 produced no persisted change\n"
    )
    assert module.classify(log).outcome_class == "no_work"
