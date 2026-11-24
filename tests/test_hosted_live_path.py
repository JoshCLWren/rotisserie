"""Hosted control room live reads and writes, run against a fake sign-in service."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from rotisserie import control_contract as contract

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "factory" / "scripts" / "build_hosted_control_room.py"
HARNESS = ROOT / "tests" / "hosted_harness.cjs"
AUTH = "https://auth.widgets.example/api/factory/github"


def builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rotisserie_hosted_builder_live", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@cache
def harness(tmp: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node not installed")
    script = Path(tmp) / "adapter.js"
    script.write_text(builder().adapter(AUTH), encoding="utf-8")
    done = subprocess.run(
        [node, str(HARNESS), str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    return result


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return harness(str(tmp_path_factory.mktemp("hosted")))


def test_page_routing_comes_from_the_shared_contract() -> None:
    page = builder().render("acme/widgets", AUTH)
    found = re.search(r"const CONTRACT = (\{.*?\});", page)
    assert found is not None
    tables = json.loads(found.group(1))
    assert set(tables["public_reads"]) == contract.PUBLIC_READS
    assert set(tables["live_reads"]) == contract.LIVE_READS
    assert set(tables["write_operations"]) == contract.WRITE_OPERATIONS
    assert tables["max_batch"] == contract.MAX_BATCH
    assert page.count("__AUTH_BASE__") == 0


def test_signed_out_keeps_public_reads_and_asks_to_sign_in(results: dict[str, Any]) -> None:
    assert results["signedOutStatus"]["ok"]
    assert results["signedOutWhy"]["error"].startswith("Sign in with GitHub to use this.")
    assert results["signedOutWrite"]["error"] == "Sign in with GitHub to use this."
    assert results["signedOutCalls"] == []
    assert "Sign in to use live views" in results["signedOutIdentity"]


def test_signed_out_model_history_is_never_selectable(results: dict[str, Any]) -> None:
    models = results["signedOutModels"]["value"]["models"]
    assert models and all(m["offered"] is False and m["historical"] for m in models)


def test_signed_in_reads_batch_into_one_live_run(results: dict[str, Any]) -> None:
    assert results["signedInIdentity"].startswith("Signed in with GitHub as admin")
    assert results["readDispatches"] == [
        {
            "operation": "read",
            "reads": [{"name": "why", "query": {}}, {"name": "leases", "query": {}}],
        }
    ]
    assert results["leases"]["value"]["source"] == "neon"
    source = results["why"]["value"]["_source"]
    assert source["kind"] == "live"
    assert source["run_url"].endswith("/actions/runs/100")
    assert source["observed_at"] == "2026-11-24T15:00:00+00:00"
    assert results["dispatchesAfterCachedRead"] == 1
    assert results["liveLine"].startswith("Live data from GitHub run 100")


def test_invalid_models_are_refused_before_dispatch(results: dict[str, Any]) -> None:
    assert "not in the live model list" in results["badModel"]["error"]
    assert "turned off" in results["offModel"]["error"]
    assert results["dispatchesAfterRejectedModels"] == ["read", "read"]
    assert "does not support shell" in results["unknownOperation"]["error"]


def test_writes_report_the_real_operator_outcome(results: dict[str, Any]) -> None:
    good = results["goodRun"]["value"]
    assert good["operator"] == {
        "run_url": "https://github.com/acme/widgets/actions/runs/102",
        "conclusion": "success",
    }
    assert good["scheduler_url"].endswith("/actions/runs/9")
    failure = results["invalidModelRun"]["error"]
    assert failure.startswith("Invalid provider/model identifier.")
    assert "actions/runs/100 failure" in failure
    assert "before Rotisserie returned a result" in results["noArtifact"]["error"]


def test_results_for_another_request_are_refused(results: dict[str, Any]) -> None:
    assert "another request" in results["mismatch"]["error"]


def test_old_sign_in_service_is_reported_as_accepted_only(results: dict[str, Any]) -> None:
    assert results["oldServiceWrite"]["value"]["result"].startswith("accepted by GitHub")
    assert results["oldServiceRead"]["error"] == "Unsupported factory operation"


def test_old_sign_in_service_keeps_the_launcher_on_history(results: dict[str, Any]) -> None:
    providers = results["oldServiceProviders"]["value"]
    assert providers["historical"] and providers["providers"]
    assert providers["live_error"] == "Unsupported factory operation"
    models = results["oldServiceModels"]["value"]["models"]
    assert all(m["offered"] is False for m in models)
    assert results["oldServiceAutoRun"]["value"]["result"].startswith("accepted by GitHub")
    assert results["oldServiceLiveLine"].startswith("Live models unavailable")


def test_expired_session_fails_closed(results: dict[str, Any]) -> None:
    assert "expired" in results["expired"]["error"]
    assert results["expiredSignedInClass"] is False
    assert "expired" in results["expiredIdentity"]
    assert results["afterExpiry"]["error"].startswith("Sign in with GitHub to use this.")
    assert "unhandledRejection" not in results


def test_neon_estimate_starts_then_polls_without_caching_pending(results: dict[str, Any]) -> None:
    started = results["estimateNeon"]["value"]
    assert started["bridge"]["run_id"] == 77
    assert started["operator"]["conclusion"] == "success"
    assert results["estimatePending"]["value"]["result"] == "pending"
    assert results["estimateDone"]["value"]["result"] == "completed"
    assert results["estimateReadRuns"] == 2


def test_signed_out_reads_published_db_usage(results: dict[str, Any]) -> None:
    found = results["signedOutDbUsage"]["value"]
    assert found["status"] == "warn" and found["label"] == "not in use yet"
    assert found["source"] == "published"
    findings = results["signedOutDoctor"]["value"]["findings"]
    assert findings[-1]["check"] == "Database usage (published)"
    assert findings[-1]["status"] == "warn"
