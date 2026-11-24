"""Same-run failover stops at an account-wide quota (issues #123 and #234).

DAILY_REFUSAL copies comic-pile run 44908459888 (Nov 21) byte for byte: OpenCode
retries the 429 eleven times and then prints an ANSI-coloured refusal.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
OUTCOME = SCRIPTS / "factory-worker-outcome.sh"
WORKER = SCRIPTS / "free-model-factory-worker.sh"
sys.path.insert(0, str(SCRIPTS))

import factory_quota_reset as reset  # noqa: E402
from factory_model_failover import ExhaustedCandidatesError, select_next_fleet  # noqa: E402

DAILY_REFUSAL = (
    "\x1b[93m\x1b[1m! \x1b[0mretrying (attempt 11): Rate limit exceeded: "
    "free-models-per-day-high-balance. \n"
    "\x1b[91m\x1b[1mError: \x1b[0mRate limit exceeded: free-models-per-day-high-balance. \n"
)
DAILY_ERROR_EVENT = json.dumps(
    {"type": "error", "error": {"message": "Rate limit exceeded: free-models-per-day."}}
)
THROTTLE = "\x1b[91m\x1b[1mError: \x1b[0mHTTP 429 Too Many Requests\n"
OUT_OF_CREDITS = (
    "Error: You’re out of credits — this request needs $0.4213. Add credits to keep going\n"
)
RESET_AT = 1795392000
OPENROUTER = [f"generated/free-{i}:free" for i in range(5)]


def _catalog(*models: str) -> dict:
    return {"data": [{"id": model, "supported_parameters": ["tools"]} for model in models]}


def _trip(
    tmp_path: Path, source: str, model: str, outcome: str, text: str, catalogs: dict | None = None
) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    runner = tmp_path / "runner"
    runner.mkdir()
    (runner / "factory-fleet-catalogs.json").write_text(json.dumps(catalogs or {}))
    worker = f"account-trip-{tmp_path.name}"
    log = Path(f"/tmp/opencode-factory-{worker}.log")
    log.write_text(text)
    script = (
        f'set -Eeuo pipefail\nsource "{OUTCOME}"\n'
        'log() { printf "LOG %s\\n" "$*"; }\n'
        f'WORKER="{worker}"; RUNNER_TEMP="{runner}"; FACTORY_TRUSTED_FAILOVER_DIR="{SCRIPTS}"\n'
        "FAST_FAILOVER_SECONDS=180\n"
        "exhausted_pools=() exhausted_accounts=() exhausted_account_details=()\n"
        f'trip_exhausted_capacity {source} "{model}" {outcome} 85\n'
        'echo "scope=$failure_scope pools=${exhausted_pools[*]:-} '
        'accounts=${exhausted_accounts[*]:-}"\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    try:
        return subprocess.run(
            ["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True, check=True
        ).stdout
    finally:
        log.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "outcome,text",
    [
        ("provider_quota", DAILY_REFUSAL),
        ("provider_quota", DAILY_ERROR_EVENT),
        ("provider_throttle", DAILY_ERROR_EVENT),
    ],
)
def test_openrouter_daily_refusal_trips_the_whole_account(
    tmp_path: Path, outcome: str, text: str
) -> None:
    out = _trip(tmp_path, "openrouter-free", OPENROUTER[0], outcome, text)
    assert "scope=model pools= accounts=openrouter-free" in out
    assert (
        f"FACTORY_ACCOUNT_QUOTA_EXHAUSTED: source=openrouter-free kind=daily model={OPENROUTER[0]}"
    ) in out


def test_throttle_stays_on_the_exact_model(tmp_path: Path) -> None:
    out = _trip(tmp_path, "openrouter-free", OPENROUTER[0], "provider_throttle", THROTTLE)
    assert "scope=model pools= accounts=\n" in out


def test_model_scoped_daily_quota_stays_on_the_exact_model(tmp_path: Path) -> None:
    text = "Error: Rate limit reached for model on requests per day (RPD): Limit 1000\n"
    out = _trip(tmp_path, "groq-free", "generated/groq-model", "provider_quota", text)
    assert "scope=model pools= accounts=\n" in out


def test_credit_pool_keeps_the_234_path(tmp_path: Path) -> None:
    catalog = {"data": [{"id": "synthetic/paid", "pricing": {"prompt": "0.1", "completion": "1"}}]}
    out = _trip(
        tmp_path,
        "orcarouter",
        "synthetic/paid",
        "provider_quota",
        OUT_OF_CREDITS,
        {"orcarouter": catalog},
    )
    assert "scope=pool pools=orcarouter accounts=\n" in out


def _fleet(attempted: set, accounts: frozenset, catalogs: dict) -> dict:
    return select_next_fleet(
        "77",
        "openrouter-free",
        attempted,
        set(),
        {},
        catalogs,
        set(catalogs),
        proven=set(),
        exhausted_accounts=accounts,
    )


def test_exhausted_account_skips_every_sibling_and_fails_over_across_providers() -> None:
    catalogs = {"openrouter-free": _catalog(*OPENROUTER), "nvidia": _catalog("independent/coder")}
    attempted = {("openrouter-free", OPENROUTER[0])}
    pick = _fleet(attempted, frozenset({"openrouter-free"}), catalogs)
    assert (pick["source"], pick["model"]) == ("nvidia", "independent/coder")
    with pytest.raises(ExhaustedCandidatesError):
        _fleet(
            attempted,
            frozenset({"openrouter-free"}),
            {"openrouter-free": catalogs["openrouter-free"]},
        )


def test_model_scoped_refusal_still_fails_over_to_a_sibling() -> None:
    catalogs = {"openrouter-free": _catalog(*OPENROUTER), "nvidia": _catalog("independent/coder")}
    pick = _fleet({("openrouter-free", OPENROUTER[0])}, frozenset(), catalogs)
    assert pick["source"] == "openrouter-free" and pick["model"] in OPENROUTER[1:]


def test_worker_passes_exhausted_accounts_to_the_selector() -> None:
    text = WORKER.read_text(encoding="utf-8")
    selector = text.split("select_next_eligible_model() {", 1)[1].split("\n}\n", 1)[0]
    assert (
        'for provider in "${exhausted_accounts[@]}"; do '
        'select_args+=(--exhausted-account "$provider"); done'
    ) in selector


def _record(tmp_path: Path, source: str, prior: str) -> tuple[str, str]:
    outcome = tmp_path / "outcome"
    outcome.write_text(prior)
    until = datetime.fromtimestamp(RESET_AT, UTC)
    detail = f"daily\t{RESET_AT}\t{reset.capacity_sentence('openrouter-free', 'daily', until)}"
    script = (
        f'source "{OUTCOME}"\nlog() {{ printf "LOG %s\\n" "$*"; }}\n'
        f'TERMINAL_OUTCOME_FILE="{outcome}"; FACTORY_TRUSTED_FAILOVER_DIR="{SCRIPTS}"\n'
        f"SOURCE={source}\nexhausted_pools=()\nexhausted_accounts=(openrouter-free)\n"
        f"exhausted_account_details=($'{detail}')\nrecord_exhausted_capacity\n"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return result.stdout, outcome.read_text()


def test_exhausted_account_ends_the_run_with_one_line_and_the_reset(tmp_path: Path) -> None:
    out, recorded = _record(tmp_path, "openrouter-free", "provider_quota\tprior\n")
    sentence = "OpenRouter daily free allowance used up; resets at Nov 22, 6:00 PM CT"
    assert (
        "LOG FACTORY_CAPACITY_EXHAUSTED: account=openrouter-free kind=daily "
        "earliest_retry_at=1795392000"
    ) in out
    assert f"::error::{sentence}" in out
    assert recorded == f"provider_quota\t{sentence}; failover ended in this run\n"


def test_exhausted_account_is_not_reported_for_another_terminal_source(tmp_path: Path) -> None:
    out, recorded = _record(tmp_path, "nvidia", "provider_failure\tnvidia down\n")
    assert "FACTORY_CAPACITY_EXHAUSTED" not in out
    assert recorded == "provider_failure\tnvidia down\n"


def test_sentence_uses_central_time() -> None:
    summer = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
    assert reset.capacity_sentence("openrouter-free", "daily", summer) == (
        "OpenRouter daily free allowance used up; resets at Jul 1, 7:00 PM CT"
    )
    assert reset.capacity_sentence("mistral", "monthly", summer).startswith(
        "mistral monthly quota used up; resets at"
    )


def _dynamic():
    spec = importlib.util.spec_from_file_location(
        "factory_dynamic_outcome_account", SCRIPTS / "factory_dynamic_outcome.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _job_log(worker: str, *, exhausted: bool, reset_at: int = RESET_AT) -> str:
    stamp = "2026-11-22T15:24:10.4236097Z"
    model = OPENROUTER[0]
    prefix = f"{stamp} [factory:{worker}][source:openrouter-free][model:{model}]"
    log = (
        f"{stamp} Factory {worker} assigned dynamic provider slot: "
        "source=openrouter-free configured=true\n"
        f"{prefix} checked out pr #34 on factory/work\n"
        f"{prefix} FACTORY_EXECUTION_CANDIDATE: source=openrouter-free model={model} "
        f"runtime=openrouter/{model}\n"
        f"{stamp} \x1b[91m\x1b[1mError: \x1b[0mRate limit exceeded: "
        "free-models-per-day-high-balance.\n"
        f"{prefix} agent exit status 1 for pr #34\n"
        f"{prefix} FACTORY_QUOTA_RESET: source=openrouter-free model={model} "
        f"outcome=provider_quota kind=daily reset_at={reset_at} reset_source=policy\n"
        f"{prefix} FACTORY_TRUSTED_FAILOVER: source=openrouter-free model={model} "
        "outcome=provider_quota scope=model\n"
    )
    if exhausted:
        log += (
            f"{prefix} FACTORY_CAPACITY_EXHAUSTED: account=openrouter-free kind=daily "
            "earliest_retry_at=1795392000\n"
            f"{stamp} ##[error]OpenRouter daily free allowance used up; "
            "resets at Nov 22, 6:00 PM CT\n"
        )
    return log


def test_classifier_records_no_capacity_with_the_reset() -> None:
    dynamic = _dynamic()
    result = dynamic.classify(_job_log("10001", exhausted=True))
    assert result["outcome_class"] == "no_capacity"
    assert result["detail"] == (
        "OpenRouter daily free allowance used up; resets at Nov 22, 6:00 PM CT"
    )
    assert dynamic.classify(_job_log("10001", exhausted=False))["outcome_class"] == "provider_quota"


def test_failures_view_counts_the_annotation_as_no_capacity() -> None:
    dynamic = _dynamic()
    assert dynamic.capacity_reason(
        "OpenRouter daily free allowance used up; resets at Nov 22, 6:00 PM CT"
    )
    assert not dynamic.capacity_reason("Rate limit exceeded: free-models-per-day-high-balance.")


@pytest.mark.parametrize("terminal", ["provider_quota", "no_capacity"])
@pytest.mark.parametrize("proven", [True, False])
def test_no_capacity_keeps_the_canonical_account_cooldown(
    tmp_path, monkeypatch, terminal: str, proven: bool
) -> None:
    import factory_state_operations as operations
    import factory_turso_operations
    from factory_state_ledger import refine_trusted_failover_models

    from tests import turso_support as support

    monkeypatch.setenv("FACTORY_SLOTS_PER_PROVIDER", "1")
    monkeypatch.delenv("FACTORY_SLOT_PROVIDER_SOURCES", raising=False)
    worker = next(
        w
        for w, (s, _) in sorted(factory_turso_operations.roster().items())
        if s == "openrouter-free"
    )
    outcome = {
        "worker": worker,
        "source": "openrouter-free",
        "model": OPENROUTER[0],
        "outcome_class": terminal,
        "exact_model_proven": proven,
        "persisted_work": False,
        "configured": True,
    }
    reset_at = int(reset.next_daily_reset(datetime.now(UTC)).timestamp())
    log = _job_log(worker, exhausted=True, reset_at=reset_at)
    refined = refine_trusted_failover_models(outcome, log)
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        assert operations.record_runtime(repo, 4002, refined) is True
        rows = db.read(
            "SELECT next_retry_at, detail FROM factory_backoff WHERE backoff_key = :k",
            {"k": f"factory-provider-backoff-v1:{repo}:source:openrouter-free"},
        )
    assert len(rows) == 1
    assert rows[0]["next_retry_at"] == reset_at
    assert json.loads(rows[0]["detail"]) == {
        "kind": "daily",
        "reset_source": "policy",
        "scope": "account",
    }
