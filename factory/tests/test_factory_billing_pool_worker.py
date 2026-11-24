"""Worker classification, pool trip wiring and trusted persistence for issue #234."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import uuid
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
WORKER = SCRIPTS / "free-model-factory-worker.sh"
OUTCOME = SCRIPTS / "factory-worker-outcome.sh"
sys.path.insert(0, str(SCRIPTS))

OUT_OF_CREDITS = (
    "Error: You\u2019re out of credits \u2014 this request needs $0.4213. Add credits to keep going\n"
)
CATALOG = {
    "data": [
        {"id": "synthetic/paid", "pricing": {"prompt": "0.00001", "completion": "0.00005"}},
        {"id": "synthetic/zero", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "synthetic/unpriced"},
    ]
}


def _classify(tmp_path: Path, text: str, status: int = 1) -> str:
    worker = f"pool-test-{uuid.uuid4().hex[:12]}"
    log = Path(f"/tmp/opencode-factory-{worker}.log")
    outcome = tmp_path / "outcome"
    log.write_text(text)
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                'SOURCE=orcarouter\nWORKER="$1"\nTERMINAL_OUTCOME_FILE="$2"\n'
                f'source "{OUTCOME}"\nrecord_agent_failure_outcome {status}\n',
                "_",
                worker,
                str(outcome),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return outcome.read_text().split("\t", 1)[0]
    finally:
        log.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "text,expected",
    [
        (OUT_OF_CREDITS, "provider_quota"),
        (
            "You're out of credits - this request needs $0.12. Add credits to keep going",
            "provider_quota",
        ),
        ("insufficient balance or no resource package. please recharge", "provider_quota"),
        ("HTTP 429 Too Many Requests", "provider_throttle"),
        ("HTTP 401 Unauthorized: invalid api key", "unknown_failure"),
        ("HTTP 410 Gone", "model_retired_410"),
        ("Error: unknown model synthetic/paid", "model_unavailable"),
        ("tests failed: 3 assertions", "unknown_failure"),
    ],
)
def test_out_of_credits_is_quota_and_other_classes_stay_distinct(
    tmp_path: Path,
    text: str,
    expected: str,
) -> None:
    assert _classify(tmp_path, text) == expected


def test_zero_exit_out_of_credits_is_an_account_refusal() -> None:
    text = WORKER.read_text(encoding="utf-8")
    assert "ACCOUNT_REFUSAL_RE='out of credits|" in text


def _trip(
    tmp_path: Path, model: str, elapsed: int, *, dirty: bool = False
) -> subprocess.CompletedProcess:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if dirty:
        (repo / "edit.txt").write_text("edited\n")
    runner = tmp_path / "runner"
    runner.mkdir()
    (runner / "factory-fleet-catalogs.json").write_text(json.dumps({"orcarouter": CATALOG}))
    worker = f"pool-trip-{tmp_path.name}"
    log = Path(f"/tmp/opencode-factory-{worker}.log")
    log.write_text(OUT_OF_CREDITS)
    script = (
        f'set -Eeuo pipefail\nsource "{OUTCOME}"\n'
        'log() { printf "LOG %s\\n" "$*"; }\n'
        f'WORKER="{worker}"; RUNNER_TEMP="{runner}"; FACTORY_TRUSTED_FAILOVER_DIR="{SCRIPTS}"\n'
        "FAST_FAILOVER_SECONDS=180; exhausted_pools=()\n"
        f'if trip_exhausted_credit_pool orcarouter "{model}" {elapsed}; then echo tripped; fi\n'
        'echo "pools=${exhausted_pools[*]:-}"\n'
    )
    try:
        return subprocess.run(
            ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=True
        )
    finally:
        log.unlink(missing_ok=True)


def test_worker_trips_pool_only_for_a_fast_clean_refusal_on_a_priced_model(tmp_path: Path) -> None:
    result = _trip(tmp_path, "synthetic/paid", 4)
    assert "tripped" in result.stdout
    assert "pools=orcarouter" in result.stdout
    assert (
        "FACTORY_CREDIT_POOL_EXHAUSTED: source=orcarouter pool=credit_balance model=synthetic/paid"
    ) in result.stdout


@pytest.mark.parametrize(
    "model,elapsed,dirty",
    [
        ("synthetic/zero", 4, False),
        ("synthetic/unpriced", 4, False),
        ("synthetic/paid", 900, False),
        ("synthetic/paid", 4, True),
    ],
)
def test_worker_keeps_unproven_refusals_on_the_exact_pair(
    tmp_path: Path,
    model: str,
    elapsed: int,
    dirty: bool,
) -> None:
    result = _trip(tmp_path, model, elapsed, dirty=dirty)
    assert "tripped" not in result.stdout
    assert "pools=\n" in result.stdout


def test_worker_wires_the_pool_before_the_next_inference_call() -> None:
    text = WORKER.read_text(encoding="utf-8")
    selector = text.split("select_next_eligible_model() {", 1)[1].split("\n}\n", 1)[0]
    assert (
        'for provider in "${exhausted_pools[@]}"; do '
        'select_args+=(--exhausted-credit-pool "$provider"); done'
    ) in selector
    clause = text.split('attempted_pairs+=("$(jq -nc --arg source "$SOURCE"', 2)[2]
    clause = clause.split("break\n        ;;", 1)[0]
    trip = clause.index('trip_exhausted_credit_pool "$SOURCE" "$MODEL" "$attempt_elapsed"')
    assert clause.index("failure_scope=model") < trip
    assert (
        trip
        < clause.index("record_model_failover_failure")
        < clause.index("if select_next_eligible_model; then")
    )
    assert clause.index("continue\n          fi") < clause.index("record_exhausted_capacity")
    assert 'blocked_providers+=("$SOURCE")' not in clause
    assert "exhausted_pools=()" in text


def test_exhausted_capacity_reports_canonical_retry_lower_bound(tmp_path: Path) -> None:
    outcome = tmp_path / "outcome"
    outcome.write_text("provider_quota\tprior\n")
    script = (
        f'source "{OUTCOME}"\nlog() {{ printf "LOG %s\\n" "$*"; }}\n'
        f'TERMINAL_OUTCOME_FILE="{outcome}"; FACTORY_TRUSTED_FAILOVER_DIR="{SCRIPTS}"\n'
        "exhausted_pools=(orcarouter)\nrecord_exhausted_capacity\n"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert "FACTORY_CAPACITY_EXHAUSTED: pools=orcarouter earliest_retry_at=20" in result.stdout
    recorded = outcome.read_text()
    assert recorded.startswith("provider_quota\tpaid credit balance exhausted at orcarouter;")
    assert "paid models rechecked at 20" in recorded

    outcome.write_text("control_plane_failure\tcatalog unreadable\n")
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert outcome.read_text().startswith("control_plane_failure\t")


def _marker(worker: str, model: str, outcome: str, scope: str) -> str:
    return (
        f"2026-11-24T00:51:20.0000000Z [factory:{worker}][source:orcarouter][model:{model}] "
        f"FACTORY_TRUSTED_FAILOVER: source=orcarouter model={model} "
        f"outcome={outcome} scope={scope}\n"
    )


def test_pool_scope_survives_the_trusted_persistence_round_trip(tmp_path, monkeypatch) -> None:
    import factory_state_operations as operations
    import factory_turso_operations
    from factory_model_attempt_events import model_attempt_events
    from factory_state_ledger import refine_trusted_failover_models

    from tests import turso_support as support

    monkeypatch.setenv("FACTORY_SLOTS_PER_PROVIDER", "1")
    monkeypatch.delenv("FACTORY_SLOT_PROVIDER_SOURCES", raising=False)
    worker, (terminal_source, _) = sorted(factory_turso_operations.roster().items())[0]
    log = _marker(worker, "synthetic/paid", "provider_quota", "pool") + _marker(
        worker, "synthetic/forged", "provider_failure", "pool"
    )
    outcome = {
        "worker": worker,
        "source": terminal_source,
        "model": "generated/terminal",
        "outcome_class": "provider_quota",
        "exact_model_proven": False,
        "persisted_work": False,
        "configured": True,
    }
    refined = refine_trusted_failover_models(outcome, log)
    assert refined["model_failures"] == [
        {
            "source": "orcarouter",
            "model": "synthetic/paid",
            "outcome": "provider_quota",
            "scope": "pool",
        }
    ]
    assert operations.validate_intermediate_model_failures(
        refined["model_failures"],
        terminal_source,
    ) == [("orcarouter", "synthetic/paid", "provider_quota", "pool")]
    with pytest.raises(ValueError):
        operations.validate_intermediate_model_failures(
            [
                {
                    "source": "orcarouter",
                    "model": "m",
                    "outcome": "provider_failure",
                    "scope": "pool",
                }
            ],
            terminal_source,
        )
    stamp, prefix = "2026-11-24T00:51:19.0000000Z", f"[factory:{worker}][source:orcarouter]"
    attempt = (
        f"{stamp} {prefix}[model:synthetic/paid] FACTORY_EXECUTION_CANDIDATE: "
        "source=orcarouter model=synthetic/paid runtime=orcarouter/synthetic/paid\n"
        f"{stamp} {prefix}[model:synthetic/paid] agent exit status 1 for pr #5671\n"
    )
    events = model_attempt_events("45300344438", outcome, attempt + log)
    assert [event["outcome_class"] for event in events] == ["provider_quota"]

    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        assert operations.record_runtime(repo, 4001, refined) is True
        rows = db.read("SELECT * FROM factory_backoff WHERE repo = :r", {"r": repo})
    keys = {row["backoff_key"]: row for row in rows}
    pool = keys[f"factory-credit-pool-v1:{repo}:source:orcarouter"]
    record = json.loads(pool["detail"])
    assert record["scope"] == "pool" and pool["streak"] == 0
    assert pool["next_retry_at"] is not None
    assert not any(k.startswith("factory-model-backoff-v1:") for k in keys)
    assert f"factory-provider-backoff-v1:{repo}:source:orcarouter" not in keys
    assert not any(k.endswith("synthetic/zero") or k.endswith("synthetic/unpriced") for k in keys)


def test_terminal_classifier_scopes_out_of_credits_to_the_last_candidate() -> None:
    spec = importlib.util.spec_from_file_location(
        "factory_dynamic_outcome_pool", SCRIPTS / "factory_dynamic_outcome.py"
    )
    assert spec is not None and spec.loader is not None
    dynamic = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dynamic
    spec.loader.exec_module(dynamic)

    def candidate(source: str, model: str, body: str) -> str:
        prefix = f"[factory:10001][source:{source}][model:{model}]"
        return (
            f"{prefix} FACTORY_EXECUTION_CANDIDATE: source={source} model={model} "
            f"runtime={source}/{model}\n{body}\n{prefix} agent exit status {{}} for pr #34\n"
        )

    head = (
        "Factory 10001 assigned dynamic provider slot: source=orcarouter configured=true\n"
        "[factory:10001][source:orcarouter][model:synthetic/paid] "
        "checked out pr #34 on factory/work\n"
    )
    refused = candidate("orcarouter", "synthetic/paid", OUT_OF_CREDITS).format(1)
    assert dynamic.classify(head + refused)["outcome_class"] == "provider_quota"
    timed_out = candidate("nvidia", "other/model", "still working").format(124)
    assert dynamic.classify(head + refused + timed_out)["outcome_class"] != "provider_quota"
