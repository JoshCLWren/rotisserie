"""Regression coverage for dispatch-time factory executor selection."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "free-model-factory-run.yml"


def _dispatch_selector(workflow: str) -> str:
    """Return the dispatch-time candidate selection step body."""
    return workflow.split("- name: Select execution candidate at dispatch time", maxsplit=1)[
        1
    ].split("- name: Report selected executor heartbeat", maxsplit=1)[0]


def _lane_resolution(workflow: str) -> str:
    """Return the executor slot policy step body."""
    return workflow.split("- name: Resolve executor slot policy", maxsplit=1)[1].split(
        "- name:", maxsplit=1
    )[0]


def test_multi_provider_entry_is_restored_while_omniroute_stays_dark() -> None:
    """Native lanes remain executable; compatible providers use the registry."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    selector = _dispatch_selector(workflow)
    lane = _lane_resolution(workflow)

    assert "factory_provider_registry.py credentials --source" in lane
    assert "opencode-free|openrouter-free|kilo-auto)" in lane
    assert "factory_live_opencode.py" in selector
    assert "kilo-auto)" in selector
    assert "factory_runtime_model_select.py" in selector
    assert "factory_published_model_catalog.py" in selector
    assert "omniroute-disabled-incident" in (
        ROOT / ".github/scripts/free-model-factory-worker.sh"
    ).read_text(encoding="utf-8")
    assert "FACTORY_OMNIROUTE_ENABLED" in workflow
    assert "auto/coding:free" not in selector
    assert "auto/best-free" not in workflow
    assert "GitHub execution is OmniRoute-only" not in workflow


def test_omniroute_catalog_discovery_requires_explicit_enable() -> None:
    """OmniRoute is unconfigured (never selected) unless explicitly enabled."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    lane = _lane_resolution(workflow)
    omniroute = lane.split("omniroute-free)", 1)[1].split(";;", 1)[0]

    assert 'FACTORY_OMNIROUTE_ENABLED:-off}" =~ ^(1|on|true|yes)$' in omniroute
    assert "configured=false" in omniroute
    assert "if: steps.lane.outputs.configured == 'true'" in _dispatch_selector(workflow)
    configure = workflow.split("- name: Configure selected OmniRoute capacity", maxsplit=1)[1][:240]
    assert "vars.FACTORY_OMNIROUTE_ENABLED == 'on'" in configure


def test_openai_compatible_factory_providers_inject_opencode_config() -> None:
    """A single registry configures all compatible provider endpoints."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    registry = (ROOT / ".github" / "scripts" / "factory_provider_registry.py").read_text(encoding="utf-8")

    assert "Configure OpenAI-compatible factory provider" in workflow
    assert "factory_provider_registry.py config" in workflow
    assert "modelID" in registry
    # Price is not an eligibility rule; real work establishes callability.
    assert "zero-price" not in workflow
    for key in ("Z_AI_API_KEY", "OLLAMA_API_KEY", "MISTRAL_API_KEY", "ORCAROUTER_API_KEY", "REQUESTY_API_KEY"):
        assert f"{key}: ${{{{ secrets.{key} }}}}" in workflow


def test_new_free_api_sources_use_real_provider_models_and_credentials() -> None:
    """New source identities and keys live in the registry, not shell cases."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    registry = (ROOT / ".github" / "scripts" / "factory_provider_registry.py").read_text(encoding="utf-8")
    for source, secret in (
        ("gemini-free", "GEMINI_API_KEY"),
        ("groq-free", "GROQ_API_KEY"),
        ("aion-free", "AION_API_KEY"),
        ("cohere-eval", "COHERE_API_KEY"),
        ("typhoon-free", "TYPHOON_API_KEY"),
    ):
        assert f'"{source}": ProviderSpec(' in registry
        assert f'"{secret}"' in registry
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in workflow
        assert f'{source})' not in _lane_resolution(workflow)


def test_model_requests_are_reserved_for_the_real_work_session() -> None:
    """No redundant model inference is spent on probes or tool-canary prompts."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    prework = workflow.split("- name: Run continuous fixed-model factory session", 1)[0]
    assert "real work verifies callability" in prework
    assert "opencode run" not in prework
    assert "KILO_AUTO_FREE_OK" not in prework
    assert "/chat/completions" not in prework
    assert "wait-opencode-model.sh" not in prework


def test_selected_executor_metadata_reaches_worker_and_telemetry() -> None:
    """Provider and selected model remain attempt metadata for the worker."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    selected_source = "${{ steps.executor.outputs.source || steps.lane.outputs.source }}"
    selected_model = "${{ steps.executor.outputs.model || steps.lane.outputs.model }}"
    selected_branch = (
        "${{ steps.executor.outputs.branch_suffix || steps.lane.outputs.branch_suffix }}"
    )
    assert f"SOURCE: {selected_source}" in workflow
    assert f"FACTORY_SOURCE: {selected_source}" in workflow
    assert f"MODEL: {selected_model}" in workflow
    assert (
        "FACTORY_MODEL: ${{ steps.executor.outputs.model || steps.lane.outputs.model }}"
        in workflow
    )
    assert (
        "FACTORY_RUNTIME_MODEL: ${{ steps.executor.outputs.runtime_model || steps.lane.outputs.runtime_model }}"
        in workflow
    )
    assert f"FACTORY_BRANCH_SUFFIX: {selected_branch}" in workflow


def test_discovery_failures_publish_normalized_outcomes() -> None:
    """Discovery failures fall back across the fleet, else write an outcome."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    selector = _dispatch_selector(workflow)

    assert "factory-discovery-outcome" in selector
    assert "control_plane_failure" in selector
    fallback = selector.split("fleet_fallback() {", 1)[1].split("\n          }\n", 1)[0]
    assert "provider_failure" in fallback
    assert "no eligible fleet candidate" in fallback


def test_worker_refuses_omniroute_source_during_incident() -> None:
    """The session wrapper refuses omniroute-free while OmniRoute is dark."""
    worker = (ROOT / ".github" / "scripts" / "free-model-factory-worker.sh").read_text(
        encoding="utf-8"
    )

    assert "OmniRoute Entry is disabled for this incident" in worker
    assert "omniroute-free" in worker
    assert "OmniRoute-only" not in worker


def test_live_selector_uses_trusted_neon_exclusions_without_registry_scan() -> None:
    """Dynamic free models use a trusted Neon snapshot and a live-cost catalog."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    selector = _dispatch_selector(workflow)
    live = selector.split("opencode-free)", 1)[1].split("openrouter-free|vercel-ai-gateway)", 1)[0]
    assert "/issues/1093/comments?per_page=100" not in live
    assert '--exclusions-file "$exclusions_file"' in live
    assert 'printf \'[]\\n\' > "$comments_file"' in live
    assert 'FACTORY_MODEL_EXCLUSIONS: ${{ needs.validate_assignment.outputs.exclusions }}' in workflow
    assert 'jq -e \'type == "object" and all(.[]; type == "array")\'' in selector


def test_unavailability_health_is_persisted_from_the_log_not_a_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    """Model-unavailable, quota and 410 keep their Neon effect without #1093 snapshots."""
    import os
    import subprocess
    import sys

    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    names = [step.get("name") for step in workflow["jobs"]["factory"]["steps"]]
    assert "Report terminal heartbeat" not in names
    summary_step = next(
        step for step in workflow["jobs"]["factory"]["steps"]
        if step.get("name") == "Summarize factory attempt"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho called >> \"$CAPTURE_FILE\"\nexit 1\n")
    gh.chmod(0o755)
    outcome = tmp_path / "outcome"
    outcome.write_text("model_unavailable\tModel unavailable: opencode/ll-alpha-free\n")
    capture = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "-c", summary_step["run"]],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "WORKER": "41", "MODEL": "ll-alpha-free", "SOURCE": "opencode-free",
            "CONFIGURED": "true", "REASON": "selected-from-live-free-catalog",
            "JOB_STATUS": "failure",
            "WORKER_STATUS_FILE": str(tmp_path / "missing"),
            "DISCOVERY_OUTCOME_FILE": str(outcome),
        },
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not capture.exists()

    sys.path.insert(0, str(WORKFLOW.parents[1] / "scripts"))
    import factory_state_ledger as ledger
    import factory_state_operations as operations

    projected: list[dict[str, object]] = []
    monkeypatch.setattr(ledger, "record_events", lambda _repo, entries: len(entries))
    monkeypatch.setattr(operations, "enabled", lambda: True)
    monkeypatch.setattr(
        operations, "record_runtime",
        lambda _repo, _run, outcome: projected.append(outcome) or True,
    )
    for run, classification in enumerate(
        ("model_unavailable", "provider_quota", "model_retired_410"), start=100,
    ):
        ledger.record_runtime_outcome("owner/repo", str(run), {
            "worker": "41", "source": "opencode-free", "model": "ll-alpha-free",
            "outcome_class": classification,
        })
    assert [item["outcome_class"] for item in projected] == [
        "model_unavailable", "provider_quota", "model_retired_410",
    ]
    assert "model_unavailable" in operations.MODEL_COOLDOWNS
    assert "provider_quota" in operations.TRANSIENT
