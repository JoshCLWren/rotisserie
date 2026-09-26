"""Regression contracts for trusted fixed-model terminal handoffs."""

from pathlib import Path


WORKER = Path('.github/scripts/free-model-factory-worker.sh')


def _worker_text() -> str:
    """Return the trusted fixed-model worker source."""
    return WORKER.read_text(encoding='utf-8')


def _function_body(name: str) -> str:
    """Return one top-level shell function body from the worker."""
    text = _worker_text()
    start = text.index(f'{name}() {{')
    end = text.index('\n}\n', start)
    return text[start:end]


def test_fixed_model_prompt_reserves_merge_and_close_for_controller() -> None:
    """Review agents must not race the trusted controller by merging directly."""
    text = _worker_text()

    assert 'Do not merge or close the assigned pull request' in text
    assert 'the trusted wrapper and review controller own the final lifecycle transition' in text


def test_already_merged_reviewed_head_is_a_successful_terminal_result() -> None:
    """A PR merged during agent execution must not crash review-controller handoff."""
    text = _worker_text()

    assert 'if [[ -n "$merged_at" ]]' in text
    assert '[[ "$observed_head" == "$EXPECTED_HEAD" ]]' in text
    assert 'factory_work_result_merged:' in text
    assert 'record_terminal_outcome success "PR #${NUMBER} merged at the reviewed head during worker execution"' in text
    assert 'skipping review controller' in text


def test_worker_emits_only_canonical_terminal_outcomes() -> None:
    """Trusted synchronous evidence must use the incident taxonomy."""
    text = _worker_text()

    assert (
        'success|no_work|work_failure|provider_failure|provider_throttle|model_unavailable|model_retired_410|'
        'model_policy_violation|environment_failure|control_plane_failure|unknown_failure'
    ) in text
    assert 'TERMINAL_OUTCOME_FILE="${RUNNER_TEMP:-/tmp}/factory-discovery-outcome"' in text


def test_timed_provider_session_is_provider_failure_after_smoke() -> None:
    """A pinned provider/model session timeout must not collapse to unknown_failure."""
    text = _worker_text()

    assert 'status == 124 || status == 137 || status == 143' in text
    assert (
        'record_terminal_outcome provider_failure '
        '"pinned provider/model session timed out or was interrupted after smoke succeeded'
    ) in text

def test_kilo_run_agent_does_not_reenable_errexit_before_returning_status() -> None:
    """Kilo exit 124 must reach the outer retry/classification loop."""
    body = _function_body('run_agent')

    assert 'bash "$TRUSTED_KILO_HELPER"' in body
    assert '|| status=$?' in body
    assert 'set +e' not in body
    assert 'set -e' not in body
    assert 'return "$status"' in body


def test_throttle_and_model_missing_remain_distinct() -> None:
    """Rate limiting and model retirement have different health scopes."""
    text = _worker_text()

    assert 'record_terminal_outcome provider_throttle' in text
    assert 'record_terminal_outcome model_unavailable' in text
    assert 'HTTP[^0-9]*404' in text
    assert 'HTTP[^0-9]*410' in text


def test_model_cooldown_ends_retry_loop_after_recording_throttle() -> None:
    """Explicit OmniRoute cooldowns must not consume another agent retry."""
    # Cooldown/route adaptation remains in shared primitives for the OmniRoute path.
    text = Path('.github/scripts/free-model-factory-worker-primitives.sh').read_text(
        encoding='utf-8'
    )

    assert "cooling down|model_cooldown" in text
    assert (
        "OmniRoute reported model cooldown; ending this assignment without another retry"
        in text
    )
    assert text.index('if is_model_cooldown_failure; then') < text.index(
        "log 'transient provider/runtime interruption; allowing OmniRoute to adapt the upstream route'"
    )

def test_stream_readiness_timeout_is_provider_failure_evidence() -> None:
    """Stream readiness failures must enter the provider taxonomy."""
    text = _worker_text()

    assert 'STREAM_READINESS_TIMEOUT' in text
    assert 'stream[^\\n]*(timeout|readiness)' in text

def test_review_controller_failure_is_control_plane_failure() -> None:
    """A trusted controller exception must not be attributed to the model/provider."""
    text = _worker_text()

    assert 'controller_status=$?' in text
    assert (
        'record_terminal_outcome control_plane_failure '
        '"trusted review controller failed for PR #${NUMBER} with exit status ${controller_status}"'
    ) in text
