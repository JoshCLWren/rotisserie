"""Trusted job-log outcome refinements: exact smoke, failover and quota evidence for the store."""
from __future__ import annotations

import json
import re
from typing import Any


def refine_trusted_dynamic_models(outcome: dict[str, Any], job_log: str) -> dict[str, Any]:
    """Persist *all* exact free-model smoke failures, even after a later success.

    Only timestamped output of the executed Actions smoke step qualifies.
    The model runner cannot create privileged store records itself.
    """
    if (outcome.get("source") != "opencode-free"
            or outcome.get("persisted_work") not in (True, False)):
        return outcome
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log)
    failures: set[str] = set()
    successful = ""
    for step in re.split(r"##\[group\]Run ", clean):
        # The smoke command is a bounded pre-work invocation. Do not inspect
        # arbitrary later model-agent output for retirement evidence.
        if "Smoke candidate: opencode/" not in step:
            continue
        matches = list(re.finditer(
            r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z Smoke candidate: opencode/([\w./:-]+)\s*$",
            step,
        ))
        for i, match in enumerate(matches):
            model = match.group(1)
            fragment = step[match.end(): matches[i+1].start() if i+1 < len(matches) else len(step)]
            if re.search(
                rf"\bModel (?:opencode/)?{re.escape(model)} has been deprecated\b",
                fragment, re.IGNORECASE,
            ) or re.search(
                rf"\bModel unavailable: opencode/{re.escape(model)}\b",
                fragment, re.IGNORECASE,
            ):
                failures.add(model)
        success = re.findall(
            r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z Dynamic free-model smoke succeeded: opencode/([\w./:-]+)\s*$",
            step,
        )
        if success:
            successful = success[-1]
    if not failures and not successful:
        return outcome
    refined = dict(outcome)
    if failures:
        refined["model_failures"] = sorted(failures)[:5]
    if successful and outcome.get("exact_model_proven") is True:
        refined["model"] = successful
    return refined

def quota_reset_hints(clean_log: str, worker: str) -> list[dict[str, object]]:
    """Trusted FACTORY_QUOTA_RESET markers from this worker, last one per pair."""
    from factory_quota_reset import parse_marker

    found: dict[tuple[object, object], dict[str, object]] = {}
    for line in re.finditer(
        r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z \[factory:(\d+)\]\[source:([^\]\n]+)\]"
        r"\[model:[^\]\n]+\] (FACTORY_QUOTA_RESET: [^\n]*)$", clean_log,
    ):
        hint = parse_marker(line.group(3))
        if hint and line.group(1) == worker and hint["source"] == line.group(2):
            found[(hint["source"], hint["model"])] = hint
    return list(found.values())


def refine_trusted_failover_models(outcome: dict[str, Any], job_log: str) -> dict[str, Any]:
    """Attest exact failed pairs, account scope and final provider transitions.

    Only the completed trusted Actions job log and verified worker marker
    qualify for durable health. A prior candidate can never classify the
    terminal successful model or overwrite its delivery state.
    """
    from factory_provider_candidates import ADAPTERS

    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log)
    worker = str(outcome.get("worker") or "")
    pattern = re.compile(
        r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z "
        r"\[factory:(\d+)\]\[source:([^\]\n]+)\]\[model:[^\]\n]+\] "
        r"FACTORY_TRUSTED_FAILOVER: source=(\S+) model=([\w./:-]{1,180}) "
        r"outcome=([a-z_]+)(?: scope=(account|model|pool))?\s*$"
    )
    accepted = {
        "provider_failure", "provider_throttle", "provider_quota",
        "model_unavailable", "model_retired_410", "model_policy_violation",
    }
    failures = list(outcome.get("model_failures") or [])
    seen = {
        (str(item.get("source") or outcome.get("source") or ""), item.get("model"))
        for item in failures if isinstance(item, dict)
    }
    for match in pattern.finditer(clean):
        log_worker, log_source, source, model, classification, scope = match.groups()
        if (log_worker != worker or log_source != source
                or source not in ADAPTERS or classification not in accepted
                or (source, model) in seen):
            continue
        if scope == "account" and classification not in {"provider_quota", "provider_failure"}:
            continue
        if scope == "pool" and classification != "provider_quota":
            continue
        failures.append({
            "source": source, "model": model, "outcome": classification,
            "scope": scope or "model",
        })
        seen.add((source, model))

    # The model cannot choose an unregistered source merely by mentioning one
    # in its text. The canonical runtime source transition must appear in the
    # trusted, timestamped wrapper log before a final-source override.
    transitions = re.findall(
        r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z "
        r"\[factory:(\d+)\]\[source:([^\]\n]+)\]\[model:[^\]\n]+\] "
        r"FACTORY_FLEET_TRANSITION: source=([^\s]+) model=([^\s]+) ",
        clean,
    )
    final_source = str(outcome.get("source") or "")
    final_model = str(outcome.get("model") or "")
    trusted_switch = any(
        w == worker and log_source == source == final_source
        and model == final_model and source in ADAPTERS
        for w, log_source, source, model in transitions
    )
    result = dict(outcome)
    if failures:
        result["model_failures"] = failures
    hints = quota_reset_hints(clean, worker)
    if hints:
        result["quota_hints"] = hints
    if trusted_switch:
        result["trusted_source_switch"] = True
    return result



def refine_trusted_smoke_timeout(outcome: dict[str, Any], job_log: str) -> dict[str, Any]:
    """Attribute a 180-second pinned-model smoke timeout to the provider.

    Unlike a generic exit 124, this requires the actual Actions smoke step to
    end with exit 124. The already-classified trusted outcome must be unknown,
    pre-proof, and nonproductive. No model fault is inferred from shell source
    alone or from the runner's subsequent cleanup.
    """
    worker = re.escape(str(outcome.get("worker") or ""))
    # A wrapper-enforced execution deadline must not cool down the provider.
    # Other evidence can be present merely because the agent inspected files.
    if (worker and outcome.get("persisted_work") is not True
            and outcome.get("outcome_class") not in {
                "provider_quota", "model_unavailable", "model_retired_410",
                "model_policy_violation",
            } and re.search(
                rf"(?m)^\d{{4}}-\d{{2}}-\d{{2}}T\S+Z \[factory:{worker}\].*"
                r"agent exit status 124 for (?:issue|pr) #\d+\b",
                job_log,
            )):
        return {
            **outcome,
            "outcome": "AGENT DEADLINE",
            "outcome_class": "agent_timeout",
            "detail": "trusted worker hit its local execution deadline; no provider failure inferred",
        }
    if (
        outcome.get("outcome_class") not in ({"unknown_failure", "model_unavailable"} if outcome.get("source") == "nvidia" else {"unknown_failure"})
        or outcome.get("exact_model_proven") is True
        or outcome.get("persisted_work") is True
        or not outcome.get("model") or not outcome.get("source")
    ):
        return outcome

    # GitHub separates executed steps with a group header. Require the smoke
    # command and terminal timeout inside the same step, not just anywhere
    # in the complete worker log.
    steps = re.split(r"##\[group\]Run ", job_log)
    for step in steps:
        if (
            "timeout --signal=TERM --kill-after=10s 180s" in step
            and re.search(r"##\[error\]Process completed with exit code 124\b", step)
        ):
            return {
                **outcome,
                "outcome": "PROVIDER FAILURE",
                "outcome_class": "provider_failure",
                "detail": "pinned OpenCode model exceeded its 180-second smoke deadline",
            }
    # A dynamic runner can rotate away from its originally selected model.
    # Recover the *executed* final smoke selector only from timestamped runner
    # output in the failing Actions smoke step, never echoed shell source.
    if outcome.get("source") == "opencode-free":
        clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log)
        for step in re.split(r"##\[group\]Run ", clean):
            if not re.search(r"##\[error\]Process completed with exit code 1\b", step):
                continue
            candidates = re.findall(
                r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z Smoke candidate: opencode/([\w./:-]+)\s*$",
                step,
            )
            if not candidates:
                continue
            attempted = candidates[-1]
            if re.search(
                rf"\bModel (?:opencode/)?{re.escape(attempted)} has been deprecated\b",
                step, re.IGNORECASE,
            ) or re.search(
                rf"\bModel unavailable: opencode/{re.escape(attempted)}\b",
                step, re.IGNORECASE,
            ):
                return {
                    **outcome,
                    "model": attempted,
                    "outcome": "MODEL MISSING",
                    "outcome_class": "model_unavailable",
                    "detail": "actual final OpenCode smoke selector is unavailable or deprecated",
                }
    # A trusted NVIDIA 410 has an irreversible provider meaning. Preserve its
    # retirement in the store, rather than retrying a known-gone model after 6h.
    if (outcome.get("source") == "nvidia"
            and outcome.get("outcome_class") in {"unknown_failure", "model_unavailable"}
            and outcome.get("model")):
        model = re.escape(str(outcome["model"]))
        if re.search(
            rf"(?m)^\d{{4}}-\d{{2}}-\d{{2}}T\S+Z NVIDIA permanently retired model: {model}\s*$",
            re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log),
        ):
            return {
                **outcome,
                "outcome": "MODEL RETIRED",
                "outcome_class": "model_retired_410",
                "detail": "trusted NVIDIA probe returned HTTP 410 for the exact pinned model",
            }
    return outcome
