"""Contract checks for runtime-derived factory worker outcome classification."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".github/scripts/factory_dynamic_outcome.py"
spec = importlib.util.spec_from_file_location("factory_dynamic_outcome_tested", SOURCE)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def session(source: str, failure: str) -> str:
    worker = "10001"
    model = "synthetic/review-model"
    return (
        f"Factory {worker} assigned dynamic provider slot: source={source} configured=true\n"
        f"  FACTORY_RUNTIME_MODEL: {source}/{model}\n"
        f"[factory:{worker}][source:{source}][model:{model}] "
        "checked out pr #34 on factory/work\n"
        f"{failure}\n"
        f"[factory:{worker}][source:{source}][model:{model}] "
        "agent exit status 1 for pr #34\n"
    )


def test_dynamic_account_quota_is_real_provider_quota():
    result = module.classify(session(
        "ollama-cloud", "Error: Add usage credits or subscribe and save up to 66%",
    ))
    assert result["worker"] == "10001"
    assert result["model"] == "synthetic/review-model"
    assert result["source"] == "ollama-cloud"
    assert result["outcome_class"] == "provider_quota"
    assert not result["exact_model_proven"]


def test_dynamic_tier_denial_affects_selected_model_only():
    result = module.classify(session(
        "vercel-ai-gateway",
        "Error: Free tier users do not have access to this model. Upgrade to paid credits.",
    ))
    assert result["model"] == "synthetic/review-model"
    assert result["outcome_class"] == "model_unavailable"
    assert result["delivery_outcome"] == "none"


def test_all_candidates_cooling_is_idle_not_new_failure():
    result = module.classify(
        "Factory 10001 assigned dynamic provider slot: source=mistral configured=true\n"
        "factory catalog: mistral: catalog contained no eligible candidates outside cooldown\n"
    )
    assert result["worker"] == "10001"
    assert result["source"] == "mistral"
    assert result["outcome_class"] == "no_work"
    assert result["model"] == ""


def test_dynamic_other_errors_delegate_to_canonical_classifier():
    result = module.classify(session("pollinations", "Error: HTTP 429 Too Many Requests"))
    assert result["outcome_class"] == "provider_throttle"
    assert result["model"] == "synthetic/review-model"


def test_legacy_explicit_lock_continues_to_use_canonical_classifier():
    result = module.classify(
        "Factory 42 locked: source=mistral model=synthetic/model "
        "runtime=mistral/synthetic/model minute=:00 scheduler=test configured=true\n"
        "Error: HTTP 429 Too Many Requests\n"
    )
    assert result["worker"] == "42"
    assert result["outcome_class"] == "provider_throttle"


def test_unselected_catalog_errors_do_not_blame_entire_provider():
    result = module.classify(
        "Factory 10001 assigned dynamic provider slot: source=mistral configured=true\n"
        "factory catalog: mistral: invalid catalog JSON\n"
    )
    assert result["outcome_class"] == "control_plane_failure"
    assert result["model"] == ""


def test_monthly_api_key_budget_limit_is_account_quota_not_model_throttle():
    """A provider-level key cap cannot be fixed by rotating to another model."""
    observed = (
        "Error: API key budget limit exceeded (monthly limit). "
        "Contact your org admin. $0.01 of $0.01 used; "
        "resets 2026-12-01T00:00:00.000Z."
    )
    result = module.classify(session("generic-gateway", observed))
    assert result["source"] == "generic-gateway"
    assert result["model"] == "synthetic/review-model"
    assert result["outcome_class"] == "provider_quota"
    assert result["persisted_work"] is False
    assert result["delivery_outcome"] == "none"


def test_monthly_api_key_limit_also_applies_to_legacy_locked_sessions():
    """The two classifier paths agree without a provider/model roster."""
    result = module.classify(
        "Factory 42 locked: source=generic-gateway model=synthetic/model "
        "runtime=generic-gateway/synthetic/model minute=:00 "
        "scheduler=test configured=true\n"
        "Error: API key budget limit exceeded (monthly limit). "
        "$0.01 of $0.01 used\n"
    )
    assert result["outcome_class"] == "provider_quota"


def test_unrelated_per_model_throttle_remains_transient():
    result = module.classify(session("generic-gateway", "Error: HTTP 429 Too Many Requests"))
    assert result["outcome_class"] == "provider_throttle"


def test_terminal_candidate_uses_successful_cross_provider_identity() -> None:
    worker = "10001"
    first = "generated/model-a"
    second = "generated/model-b"
    log = (
        f"Factory {worker} assigned dynamic provider slot: source=nvidia configured=true\n"
        f"2026-11-16T19:00:00Z [factory:{worker}][source:nvidia][model:{first}] "
        f"FACTORY_EXECUTION_CANDIDATE: source=nvidia model={first} runtime=nvidia/{first}\n"
        "Error: Insufficient balance or no resource package. Please recharge.\n"
        f"2026-11-16T19:01:00Z [factory:{worker}][source:requesty][model:{second}] "
        f"FACTORY_EXECUTION_CANDIDATE: source=requesty model={second} runtime=requesty/{second}\n"
        f"2026-11-16T19:01:01Z [factory:{worker}][source:requesty][model:{second}] "
        "agent exit status 0 for issue #5000\n"
        f"2026-11-16T19:01:02Z [factory:{worker}][source:requesty][model:{second}] "
        "opened/updated PR #5001 for issue #5000\n"
    )
    result = module.classify(log)
    assert result["source"] == "requesty"
    assert result["model"] == second
    assert result["outcome_class"] != "provider_quota"


def test_zai_account_refusal_is_account_quota() -> None:
    result = module.classify(
        session("z-ai", "Error: Insufficient balance or no resource package. Please recharge.")
    )
    assert result["outcome_class"] == "provider_quota"


def _stamped(text: str) -> str:
    return "\n".join(f"2026-01-01T00:00:00.0000000Z {line}" for line in text.splitlines()) + "\n"


def test_actions_step_env_health_snapshot_is_not_runtime_evidence() -> None:
    """Echoed step env (stored history) must not classify this run as throttled.

    Every worker step echoes FACTORY_CANDIDATE_HEALTH, whose prior outcomes
    contain "provider_throttle"/"quota". A run that never reached inference
    (assignment stage changed) must not inherit those strings.
    """
    worker, source, model = "10001", "pollinations", "synthetic/vl-model"
    log = _stamped(
        f"Factory {worker} assigned dynamic provider slot: source={source} configured=true\n"
        "env:\n"
        '  FACTORY_CANDIDATE_HEALTH: {"models":[{"model":"x","outcome":"provider_throttle",'
        '"source":"nvidia"},{"model":"y","outcome":"provider_quota","source":"z-ai"}]}\n'
        "  FACTORY_RATE_LIMIT_NOTE: quota and rate limit history\n"
        "shell: /usr/bin/bash --noprofile --norc -e -o pipefail {0}\n"
        f"[factory:{worker}][source:{source}][model:{model}] assigned PR stage changed\n"
        "##[error]Process completed with exit code 3.\n"
    )
    result = module.classify(log)
    assert result["source"] == source
    assert result["outcome_class"] not in {"provider_throttle", "provider_quota"}


def test_real_throttle_after_env_dump_is_still_detected() -> None:
    log = _stamped(session("nvidia", "Error: HTTP 429 Too Many Requests"))
    log = log.replace(
        "  FACTORY_RUNTIME_MODEL", "  FACTORY_CANDIDATE_HEALTH: {}\n2026-01-01T00:00:00.0000000Z   FACTORY_RUNTIME_MODEL", 1,
    )
    assert module.classify(log)["outcome_class"] == "provider_throttle"


def test_vercel_api_key_budget_exceeded_is_account_quota() -> None:
    result = module.classify(session(
        "vercel-ai-gateway",
        "Error: API key budget exceeded. Current spend: $1.02, limit: $1.00. "
        "Team owners can edit this budget in AI Gateway Budgets & Spend",
    ))
    assert result["outcome_class"] == "provider_quota"


def test_nvidia_missing_function_is_model_unavailable() -> None:
    result = module.classify(session(
        "nvidia",
        "Error: Function id '00000000-0000-0000-0000-000000000000' version 'null': "
        "Specified function in account 'generated' is not found",
    ))
    assert result["outcome_class"] == "model_unavailable"


_TAGGED = ("agent ", "semantic", "released", "opened")


def _review_session(worker: str, source: str, model: str, body: str, kind: str = "pr",
                    number: int = 5299) -> str:
    """Real worker order: target checkout precedes the execution-candidate marker."""
    tag = f"[factory:{worker}][source:{source}][model:{model}] "
    return (
        f"Factory {worker} assigned dynamic provider slot: source={source} configured=true\n"
        f"{tag}executing control-plane assignment: {kind} #{number}; runtime {source}/{model}\n"
        f"{tag}checked out {kind} #{number} on factory/work at {'a' * 40}\n"
        f"{tag}automatic persisted work enabled for run 1 (every 45s)\n"
        f"{tag}FACTORY_EXECUTION_CANDIDATE: source={source} model={model} runtime={source}/{model}\n"
        + "".join(f"{tag}{line}\n" if line.startswith(_TAGGED) else f"{line}\n"
                  for line in body.splitlines())
    )


def test_target_checked_out_before_execution_boundary_is_kept() -> None:
    """#5275 regression: delivery and exact proof survive the candidate boundary."""
    log = _review_session("10001", "nvidia", "a/b", (
        "agent exit status 0 for issue #5000\n"
        "opened/updated PR #5001 for issue #5000\n"
        "released issue #5000 to factory:unowned at factory:review (handoff)\n"
        "released pr #5001 to factory:unowned at factory:review (handoff)"
    ), kind="issue", number=5000)
    result = module.classify(log)
    assert result["selected_kind"] == "issue"
    assert result["selected_number"] == "5000"
    assert result["exact_model_proven"] is True
    assert result["delivery_pr"] == "5001"


def test_other_workers_target_is_not_borrowed() -> None:
    log = _review_session("10001", "nvidia", "a/b", "agent exit status 0 for pr #5299")
    log = log.replace("[factory:10001][source:nvidia][model:a/b] checked out",
                      "[factory:999][source:nvidia][model:a/b] checked out")
    assert module.classify(log)["selected_number"] == ""


def test_pollinations_credit_refusal_with_zero_exit_is_provider_quota() -> None:
    """OpenCode exits 0 after Pollinations refuses for credits (run 44016101975)."""
    log = _review_session("10001", "pollinations", "openai/gpt-5.5", (
        "> build · openai/gpt-5.5\n"
        "The account behind this API key doesn't have enough credits. Please top up, then try again.\n"
        "agent exit status 0 for pr #5299\n"
        "semantic_review_recovery_failed: review model did not produce an authoritative "
        "terminal verdict for PR #5299"
    ))
    result = module.classify(log)
    assert result["outcome_class"] == "provider_quota"
    assert result["selected_kind"] == "pr"
    assert result["selected_number"] == "5299"


def test_review_without_terminal_verdict_is_pr_work_failure() -> None:
    """A clean review exit with no verdict must reach the exact-head PR backoff."""
    log = _review_session("10001", "nvidia", "a/b", (
        "> build · a/b\n"
        "agent exit status 0 for pr #5299\n"
        "semantic review needs a verdict or acceptance report; attempting one bounded "
        "same-session evidence recovery\n"
        "semantic_review_recovery_failed: same-session recovery exited 124\n"
        "semantic_review_recovery_failed: review model did not produce an authoritative "
        "terminal verdict for PR #5299\n"
        "released pr #5299 to factory:unowned at factory:review (semantic-review-recovery-failed)"
    ))
    result = module.classify(log)
    assert result["outcome_class"] == "work_failure"
    assert result["selected_kind"] == "pr"
    assert result["selected_number"] == "5299"


def test_clean_exit_prose_about_quotas_is_not_a_provider_throttle() -> None:
    """Run 44093346708: the agent read docs mentioning quota and rate limits."""
    log = _review_session("10001", "openrouter-free", "a/b:free", (
        "> build · a/b:free\n"
        "Weekly quota exhaustion is legitimate and appears as quota-limited; HTTP 429 means rate limit.\n"
        "agent exit status 0 for pr #5308\n"
        "semantic_review_missing_verdict: review model did not produce an authoritative "
        "terminal verdict for PR #5308"
    ), number=5308)
    result = module.classify(log)
    assert result["outcome_class"] == "work_failure"
    assert result["exact_model_proven"] is True


def test_failed_session_with_http_429_is_still_a_throttle() -> None:
    log = _review_session("10001", "openrouter-free", "a/b:free", (
        "Error: HTTP 429 Too Many Requests\n"
        "agent exit status 1 for pr #5308"
    ), number=5308)
    assert module.classify(log)["outcome_class"] == "provider_throttle"


def test_zero_exit_account_refusal_keeps_quota() -> None:
    log = _review_session("10001", "pollinations", "a/b", (
        "The account behind this API key doesn't have enough credits.\n"
        "agent exit status 0 for pr #5299\n"
        "agent exited 0 after a provider account refusal; treating it as a provider failure"
    ))
    assert module.classify(log)["outcome_class"] == "provider_quota"
