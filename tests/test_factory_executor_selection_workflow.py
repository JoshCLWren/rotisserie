"""Regression coverage for dispatch-time factory executor selection."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "free-model-factory-run.yml"


def _dispatch_selector(workflow: str) -> str:
    """Return the dispatch-time candidate selection step body."""
    return workflow.split(
        "- name: Select execution candidate at dispatch time", maxsplit=1
    )[1].split("- name: Report selected executor heartbeat", maxsplit=1)[0]


def test_multi_provider_entry_is_restored_while_omniroute_stays_dark() -> None:
    """Incident restore: nvidia/opencode/openrouter/kilo run; OmniRoute does not."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    selector = _dispatch_selector(workflow)

    assert "nvidia)" in workflow
    assert "opencode-free|openrouter-free)" in workflow
    assert "kilo-auto)" in workflow
    assert "z-ai|ollama-cloud)" in workflow
    assert "nvidia|kilo-auto|z-ai|ollama-cloud)" in workflow
    assert "omniroute-disabled-incident" in workflow
    assert "FACTORY_OMNIROUTE_ENABLED" in workflow
    assert "factory_provider_candidates.py" in selector
    assert "factory_candidate_health.py" in selector
    assert "auto/coding:free" not in selector
    assert "auto/best-free" not in workflow
    assert "GitHub execution is OmniRoute-only" not in workflow


def test_omniroute_catalog_discovery_requires_explicit_enable() -> None:
    """OmniRoute catalog fetch stays gated off during the incident."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    selector = _dispatch_selector(workflow)

    assert 'FACTORY_OMNIROUTE_ENABLED:-off}" =~ ^(1|on|true|yes)$' in selector
    configure = workflow.split(
        "- name: Configure selected OmniRoute capacity", maxsplit=1
    )[1][:240]
    assert "vars.FACTORY_OMNIROUTE_ENABLED == 'on'" in configure


def _nvidia_probe_step(workflow: str) -> str:
    """Return the NVIDIA pre-smoke probe step body."""
    return workflow.split(
        "- name: Probe pinned NVIDIA model before OpenCode smoke", maxsplit=1
    )[1].split("- name: Smoke exact pinned model through OpenCode", maxsplit=1)[0]


def test_openai_compatible_factory_providers_inject_opencode_config() -> None:
    """Z.AI and Ollama Cloud lanes write OmniRoute-shaped openai-compat config."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "Configure OpenAI-compatible factory provider" in workflow
    assert "https://api.z.ai/api/paas/v4" in workflow
    assert "https://ollama.com/v1" in workflow
    assert "Z_AI_API_KEY: ${{ secrets.Z_AI_API_KEY }}" in workflow
    assert "OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}" in workflow
    assert 'npm: "@ai-sdk/openai-compatible"' in workflow


def test_runtime_provider_probes_remain_authoritative() -> None:
    """Pinned NVIDIA and catalog free slots keep real probe/smoke authority."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "Probe pinned NVIDIA model before OpenCode smoke" in workflow
    assert "Smoke exact pinned model through OpenCode" in workflow
    assert "Smoke Kilo Auto Free through Kilo CLI" in workflow
    assert "integrate.api.nvidia.com" in workflow


def test_nvidia_probe_retries_http_000_and_curl_transport_timeouts() -> None:
    """Empty curl responses must retry like 429, then soft-exit instead of hard-fail."""
    probe = _nvidia_probe_step(WORKFLOW.read_text(encoding="utf-8"))

    assert "--max-time 45" in probe
    assert "--max-time 120" not in probe
    assert "chat/completions || true" not in probe
    assert "|| curl_exit=$?" in probe
    assert "curl_exit" in probe
    assert 'code="${code:-000}"' in probe
    assert '"$code" == "000"' in probe
    assert '"$curl_exit" =~ ^(6|7|28|35|52|56)$' in probe
    assert "allowing worker to proceed with built-in retry handling" in probe
    assert "factory-model-retired-410:v1" in probe
    assert "model_retired_410" in probe
    assert "provider_failure\\tNVIDIA probe HTTP" in probe
    assert "model_unavailable\\tNVIDIA probe HTTP 404" in probe
    assert "max_attempts=3" in probe
    # Transport exhaustion must not sticky-retire or write 410.
    assert probe.index('"$code" == "000"') < probe.index("allowing worker to proceed")
    assert probe.index("allowing worker to proceed") < probe.index(
        "provider_failure\\tNVIDIA probe HTTP"
    )


def test_selected_executor_metadata_reaches_worker_and_telemetry() -> None:
    """Provider and selected model remain attempt metadata for the worker."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    selected_source = (
        "${{ steps.executor.outputs.source || steps.lane.outputs.source }}"
    )
    selected_model = (
        "${{ steps.executor.outputs.model || steps.lane.outputs.model }}"
    )
    selected_runtime = (
        "${{ steps.executor.outputs.runtime_model "
        "|| steps.lane.outputs.runtime_model }}"
    )
    selected_branch = (
        "${{ steps.executor.outputs.branch_suffix "
        "|| steps.lane.outputs.branch_suffix }}"
    )
    assert f"SOURCE: {selected_source}" in workflow
    assert f"FACTORY_SOURCE: {selected_source}" in workflow
    assert f"MODEL: {selected_model}" in workflow
    assert f"FACTORY_MODEL: {selected_model}" in workflow
    assert f"FACTORY_RUNTIME_MODEL: {selected_runtime}" in workflow
    assert f"FACTORY_BRANCH_SUFFIX: {selected_branch}" in workflow


def test_discovery_failures_publish_normalized_outcomes() -> None:
    """Catalog/provider discovery failures write normalized outcome markers."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    selector = _dispatch_selector(workflow)

    assert "factory-discovery-outcome" in selector
    assert "control_plane_failure" in selector
    assert "provider_unavailable" in selector or "model_unavailable" in selector


def test_worker_refuses_omniroute_source_during_incident() -> None:
    """The session wrapper refuses omniroute-free while OmniRoute is dark."""
    worker = (
        ROOT / ".github" / "scripts" / "free-model-factory-worker.sh"
    ).read_text(encoding="utf-8")

    assert "OmniRoute Entry is disabled for this incident" in worker
    assert "omniroute-free" in worker
    assert "OmniRoute-only" not in worker
