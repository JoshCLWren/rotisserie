"""Per-lease model failover exhausts discovered candidates without a roster."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from factory_model_failover import ExhaustedCandidatesError, select_next  # noqa: E402


def _catalog(*models: str) -> dict:
    return {"data": [{"id": model, "supported_parameters": ["tools"]} for model in models]}


def test_openrouter_free_iterates_every_discovered_free_candidate() -> None:
    names = [f"sample/generated-{i}:free" for i in range(8)]
    catalog = _catalog("sample/paid", *names)
    attempted: set[str] = set()
    for _ in names:
        selected = select_next("openrouter-free", "12345", attempted, {}, catalog_json=catalog)
        model = selected["model"]
        assert model in names and model not in attempted
        assert selected["runtime_model"] == f"openrouter/{model}"
        attempted.add(model)
    assert attempted == set(names)
    with pytest.raises(ExhaustedCandidatesError):
        select_next("openrouter-free", "12345", attempted, {}, catalog_json=catalog)


def test_cooldown_is_exact_model_and_provider() -> None:
    candidates = [f"sample/model-{i}" for i in range(5)]
    catalog = _catalog(*candidates)
    excluded = {"nvidia": [candidates[0]], "orcarouter": [candidates[1]]}
    attempted: set[str] = set()
    for _ in range(len(candidates) - 1):
        selected = select_next("nvidia", "567", attempted, excluded, catalog_json=catalog)
        assert selected["model"] not in attempted
        assert selected["model"] != candidates[0]
        attempted.add(selected["model"])
    assert attempted == set(candidates[1:])
    with pytest.raises(ExhaustedCandidatesError):
        select_next("nvidia", "567", attempted, excluded, catalog_json=catalog)
    # A source/model failure must not suppress the same model on another source.
    other = select_next("orcarouter", "567", set(), excluded, catalog_json=catalog)
    assert other["model"] != candidates[1]
    assert other["model"] in candidates


def test_opencode_free_iterates_live_catalog_without_roster() -> None:
    names = [f"test-generated-{i}" for i in range(4)]
    catalog = json.dumps({"models": [{"id": name, "cost": {"input": 0, "output": 0}}
                                      for name in names]})
    attempted: set[str] = set()
    for _ in names:
        selected = select_next("opencode-free", "41", attempted, {}, catalog_text=catalog)
        assert selected["model"] in names
        assert selected["model"] not in attempted
        attempted.add(selected["model"])
    with pytest.raises(ExhaustedCandidatesError):
        select_next("opencode-free", "41", attempted, {}, catalog_text=catalog)


def test_kilo_tries_all_published_cli_selectors() -> None:
    names = [f"kilo-auto/generated-{i}" for i in range(3)]
    catalog = "\n".join(names)
    attempted: set[str] = set()
    for _ in names:
        selected = select_next("kilo-auto", "41", attempted, {}, catalog_text=catalog)
        assert selected["model"] == selected["runtime_model"]
        assert selected["model"] not in attempted
        attempted.add(selected["model"])
    with pytest.raises(ExhaustedCandidatesError):
        select_next("kilo-auto", "41", attempted, {}, catalog_text=catalog)


def test_excluded_models_never_appear_even_if_unattempted() -> None:
    catalog = _catalog("generated/a:free", "generated/b:free", "generated/c:free")
    selection = select_next(
        "openrouter-free", "99", set(),
        {"openrouter-free": ["generated/a:free", "generated/b:free"]},
        catalog_json=catalog,
    )
    assert selection["model"] == "generated/c:free"

def test_intermediate_model_outcomes_preserve_exact_failure_type() -> None:
    from factory_state_operations import validate_intermediate_model_failures

    names = [f"generated/model-{i}" for i in range(3)]
    failures = [
        {"source": "nvidia", "model": names[0], "outcome": "provider_quota", "scope": "account"},
        {"source": "orcarouter", "model": names[1], "outcome": "provider_throttle", "scope": "model"},
        {"source": "nvidia", "model": names[2], "outcome": "model_unavailable", "scope": "model"},
    ]
    assert validate_intermediate_model_failures(failures, "nvidia") == [
        ("nvidia", names[0], "provider_quota", "model"),
        ("nvidia", names[2], "model_unavailable", "model"),
        ("orcarouter", names[1], "provider_throttle", "model"),
    ]
    with pytest.raises(ValueError):
        validate_intermediate_model_failures(
            [{"model": names[0], "outcome": "control_plane_failure"}], "nvidia",
        )


def test_trusted_failover_log_preserves_all_failed_models() -> None:
    from factory_state_ledger import refine_trusted_failover_models

    worker = "123456"
    pairs = [
        ("nvidia", "generated/model-a", "provider_quota", "account"),
        ("orcarouter", "generated/model-b", "provider_throttle", "model"),
    ]
    lines = [
        f"2026-11-16T19:00:0{i}Z [factory:{worker}][source:{source}]"
        f"[model:{model}] FACTORY_TRUSTED_FAILOVER: "
        f"source={source} model={model} outcome={kind} scope={scope}"
        for i, (source, model, kind, scope) in enumerate(pairs)
    ]
    result = refine_trusted_failover_models(
        {"worker": worker, "source": "orcarouter", "model": "generated/model-c"},
        "\n".join(lines),
    )
    assert result["model_failures"] == [
        {"source": source, "model": model, "outcome": kind, "scope": scope}
        for source, model, kind, scope in pairs
    ]
    wrong = lines[0].replace("[source:nvidia]", "[source:orcarouter]")
    assert "model_failures" not in refine_trusted_failover_models(
        {"worker": worker, "source": "nvidia"}, wrong,
    )


def test_cross_provider_fallback_retains_exact_pair_identity() -> None:
    from factory_model_failover import select_next_fleet

    names = [f"generated/candidate-{i}" for i in range(6)]
    catalogs = {
        "nvidia": _catalog(*names[:2]),
        "orcarouter": _catalog(*names[2:4]),
        "pollinations": _catalog(*names[4:]),
    }
    attempted: set[tuple[str, str]] = set()
    seen: list[tuple[str, str]] = []
    for _ in range(len(names)):
        selected = select_next_fleet(
            "77", "nvidia", attempted, set(), {}, catalogs,
            set(catalogs),
        )
        pair = (selected["source"], selected["model"])
        assert pair not in attempted
        attempted.add(pair)
        seen.append(pair)
    sources = [source for source, _ in seen]
    # The assigned provider is exhausted first; each fallback provider's
    # models are then tried consecutively (provider order is hash rotation).
    assert sources[:2] == ["nvidia", "nvidia"]
    assert sorted(sources[2:4]) == sorted(sources[2:4][:1] * 2)
    assert sorted(sources[4:6]) == sorted(sources[4:6][:1] * 2)
    assert set(sources[2:]) == {"orcarouter", "pollinations"}
    assert set(seen) == {(s, item["id"]) for s in catalogs for item in catalogs[s]["data"]}
    with pytest.raises(ExhaustedCandidatesError):
        select_next_fleet("77", "nvidia", attempted, set(), {}, catalogs, set(catalogs))


def test_account_breaker_skips_all_models_and_rotates_to_other_provider() -> None:
    from factory_model_failover import select_next_fleet

    names = [f"generated/candidate-{i}" for i in range(4)]
    catalogs = {"nvidia": _catalog(*names[:3]), "orcarouter": _catalog(names[-1])}
    selected = select_next_fleet(
        "77", "nvidia", {("nvidia", names[0])}, {"nvidia"},
        {}, catalogs, set(catalogs),
    )
    assert (selected["source"], selected["model"]) == ("orcarouter", names[-1])


def test_model_throttle_does_not_block_other_models_on_source() -> None:
    from factory_model_failover import select_next_fleet

    names = [f"generated/free-{i}:free" for i in range(4)]
    catalogs = {"openrouter-free": _catalog("generated/paid", *names)}
    attempted = {("openrouter-free", names[0])}
    selected = select_next_fleet(
        "77", "openrouter-free", attempted, set(),
        {"openrouter-free": [names[1]]},
        catalogs, set(catalogs),
    )
    assert selected["source"] == "openrouter-free"
    assert selected["model"] in set(names[2:])
    assert selected["model"] != "generated/paid"


def test_fleet_cannot_choose_unadmitted_provider_even_with_catalog() -> None:
    from factory_model_failover import select_next_fleet

    catalog = _catalog("generated/candidate")
    with pytest.raises(ExhaustedCandidatesError):
        select_next_fleet(
            "77", "nvidia", {("nvidia", "generated/candidate")},
            set(), {}, {"nvidia": catalog, "orcarouter": catalog}, {"nvidia"},
        )


def test_dispatch_fallback_skips_cooling_lane_and_is_attested() -> None:
    """A lane whose catalog is fully cooling selects another fleet provider.

    The dispatch step echoes the same trusted transition marker as an
    in-lease switch, so the outcome projector accepts the new final source.
    """
    from factory_model_failover import select_next_fleet
    from factory_state_ledger import refine_trusted_failover_models

    names = [f"generated/candidate-{i}" for i in range(3)]
    catalogs = {"nvidia": _catalog(names[0]), "pollinations": _catalog(*names[1:])}
    selected = select_next_fleet(
        "9001", "nvidia", set(), {"nvidia"}, {"nvidia": [names[0]]},
        catalogs, set(catalogs),
    )
    assert selected["source"] == "pollinations"
    marker = (
        f"2026-01-01T00:00:00.0000000Z [factory:9001][source:{selected['source']}]"
        f"[model:{selected['model']}] FACTORY_FLEET_TRANSITION: "
        f"source={selected['source']} model={selected['model']} "
        f"runtime={selected['runtime_model']} target=dispatch\n"
    )
    refined = refine_trusted_failover_models(
        {"worker": "9001", "source": selected["source"], "model": selected["model"]},
        marker,
    )
    assert refined["trusted_source_switch"] is True


def test_dispatch_selection_falls_back_across_fleet_in_workflow() -> None:
    workflow = (ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    step = workflow.split("- name: Select execution candidate at dispatch time", 1)[1]
    step = step.split("- name:", 1)[0]
    assert "fleet_fallback()" in step
    assert '--blocked-provider "$SOURCE"' in step
    assert "FACTORY_FLEET_TRANSITION: source=$selected_source" in step
    assert 'echo "source=$selected_source"' in step
    # Every lane-specific failure path now falls back instead of exiting.
    assert step.count("fleet_fallback '") >= 5
    assert "steps.executor.outputs.configure_adapter" in workflow


def test_worker_fails_over_on_fast_unclassified_failure_without_releasing() -> None:
    """A candidate that dies before doing work advances within the same lease.

    Production evidence: NVIDIA non-chat catalog entries fail in seconds with
    errors no narrower class matched; the worker then released the lease
    instead of trying the next eligible pair.
    """
    worker = (ROOT / ".github/scripts/free-model-factory-worker.sh").read_text()
    loop = worker.split('log "FACTORY_EXECUTION_CANDIDATE:', 1)[1]
    fast = loop.index('[[ "$failure_class" == unknown_failure ]]')
    failover = loop.index("provider_quota|provider_throttle|provider_failure|")
    assert fast < failover
    clause = loop[fast:failover]
    assert "attempt_elapsed < FAST_FAILOVER_SECONDS" in clause
    assert '-z "$(git status --porcelain)"' in clause
    assert "agent_status != 124" in clause
    assert "failure_class=provider_failure" in clause


def test_proven_model_is_preferred_but_never_overrides_cooldown() -> None:
    from factory_model_failover import select_next

    names = [f"generated/candidate-{i}" for i in range(6)]
    catalog = _catalog(*names)
    proven = {("nvidia", names[4]), ("nvidia", names[5])}
    for worker in ("1", "2", "3", "4"):
        picked = select_next("nvidia", worker, set(), {}, catalog_json=catalog, proven=proven)
        assert picked["model"] in {names[4], names[5]}
    # A cooled proven model stays excluded; the other proven one is chosen.
    cooled = select_next(
        "nvidia", "1", set(), {"nvidia": [names[4]]}, catalog_json=catalog, proven=proven,
    )
    assert cooled["model"] == names[5]
    # Once every proven model is attempted, unproven ones remain eligible.
    rest = select_next(
        "nvidia", "1", {names[4], names[5]}, {}, catalog_json=catalog, proven=proven,
    )
    assert rest["model"] in set(names[:4])
    with pytest.raises(ExhaustedCandidatesError):
        select_next("nvidia", "1", {names[4], names[5]}, {}, catalog_json=catalog,
                    proven=proven, proven_only=True)


def test_fleet_prefers_proven_model_on_another_provider() -> None:
    from factory_model_failover import select_next_fleet

    catalogs = {"nvidia": _catalog("generated/a", "generated/b"),
                "pollinations": _catalog("generated/c")}
    picked = select_next_fleet(
        "9", "nvidia", set(), set(), {}, catalogs, set(catalogs),
        proven={("pollinations", "generated/c")},
    )
    assert (picked["source"], picked["model"]) == ("pollinations", "generated/c")
    # Without proven evidence the original provider still goes first.
    plain = select_next_fleet("9", "nvidia", set(), set(), {}, catalogs, set(catalogs), proven=set())
    assert plain["source"] == "nvidia"


def test_failover_into_opencode_reports_source() -> None:
    """Regression: OpenCode selections lacked .source, crashing in-lease failover."""
    from factory_model_failover import select_next_fleet

    catalog = json.dumps({"models": [{"id": "generated-free"}]})
    picked = select_next_fleet(
        "9", "nvidia", set(), set(), {}, {"opencode-free": catalog}, {"opencode-free"},
        proven=set(),
    )
    assert picked["source"] == "opencode-free"
    assert picked["model"] == "generated-free"


def test_proven_models_reads_only_real_work_outcomes() -> None:
    from factory_model_failover import proven_models

    snapshot = json.dumps({"models": [
        {"source": "nvidia", "model": "a", "status": "healthy", "outcome": "success"},
        {"source": "nvidia", "model": "b", "status": "healthy", "outcome": "work_failure"},
        {"source": "nvidia", "model": "c", "status": "cooling", "outcome": "provider_throttle"},
        {"source": "nvidia", "model": "d", "status": "retired", "outcome": "success"},
        {"source": "nvidia", "model": "e", "status": "healthy", "outcome": "no_work"},
    ]})
    assert proven_models(snapshot) == {("nvidia", "a"), ("nvidia", "b")}
    assert proven_models("not json") == set()
    assert proven_models("") == set()


def test_opencode_v2_fleet_discovery_and_selection(tmp_path, monkeypatch):
    from factory_model_failover import discover_fleet_catalogs, select_next_fleet
    import subprocess
    import shutil

    called = []
    catalog = "opencode/generated-free\nother-provider/unrelated\n"
    def fake_run(args, **kwargs):
        called.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout=catalog)
    monkeypatch.setattr(shutil, "which", lambda _: "/tmp/opencode")
    monkeypatch.setattr(subprocess, "run", fake_run)
    catalogs = discover_fleet_catalogs(
        {"opencode-free"}, cache=tmp_path / "catalog.json", env={})
    assert called == [["opencode", "models", "--standalone"]]
    picked = select_next_fleet(
        "77", "opencode-free", set(), set(), {}, catalogs, {"opencode-free"},
        proven=set())
    assert picked["runtime_model"] == "opencode/generated-free"


def test_worker_never_blocks_provider_for_model_credit_error():
    worker = (ROOT / ".github/scripts/free-model-factory-worker.sh").read_text()
    clause = worker.split('attempted_pairs+=("$(jq -nc --arg source "$SOURCE"', 1)[1]
    clause = clause.split("record_model_failover_failure", 1)[0]
    assert "failure_scope=model" in clause
    assert "failure_scope=account" not in clause
    assert 'blocked_providers+=("$SOURCE")' not in clause
