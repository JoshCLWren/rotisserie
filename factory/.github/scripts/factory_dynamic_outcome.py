#!/usr/bin/env python3
"""Classify live provider-derived worker sessions with the canonical legacy rules.

The model roster was removed in #5213; the trusted Actions collector now sees
a dynamic slot marker rather than the historical "Factory N locked" marker.
This adapter restores the equivalent metadata before calling the existing
classifier. Only executed provider rejection text can refine its outcome.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

_CLASSIFIER = Path(__file__).with_name("classify-fixed-model-run.py")
_SLOT = re.compile(
    r"Factory (?P<worker>\d+) assigned dynamic provider slot: "
    r"source=(?P<source>\S+) configured=(?P<configured>true|false)", re.I
)
_MODEL = re.compile(
    r"\[factory:(?P<worker>\d+)\]\[source:(?P<source>[^\]\n]+)\]"
    r"\[model:(?P<model>[^\]\n]+)\]"
)
_COOLING = re.compile(
    r"factory catalog: [^\n]+: catalog contained no eligible candidates outside cooldown", re.I
)
_ACCOUNT_CREDITS = re.compile(
    r"add usage credits or subscribe|api key budget (?:limit )?exceeded|insufficient balance or no resource package|please recharge|"
    # Pollinations: OpenCode prints this refusal and still exits 0.
    r"(?:does(?:n't|n\u2019t| not)|do(?:n't|n\u2019t| not)) have enough credits",
    re.I,
)
_MODEL_ACCESS = re.compile(
    r"free tier users do not have access to this model|"
    # NVIDIA lists catalog entries (embedding, CLIP, safety, translation
    # NIMs) whose chat function is absent for this account.
    r"function id \S+ version \S+: specified function .*not found",
    re.I,
)
# A semantic review that ended without an authoritative verdict is the PR's
# work failure (so project_runtime applies the exact-head PR retry backoff),
# never a healthy idle model. The status names come from
# factory-semantic-verdict.sh.
_MISSING_VERDICT = re.compile(
    r"\]\s+semantic_review_(?:missing_verdict|recovery_failed|conflicting_verdict): "
    r"review model did not produce an authoritative terminal verdict for PR #(?P<pr>\d+)\b"
)
# GitHub Actions echoes each step's `env:`/`with:` block as indented
# `NAME: value` lines. They carry trusted history snapshots such as
# FACTORY_CANDIDATE_HEALTH whose stored outcome strings ("provider_throttle",
# "quota") describe earlier runs, never this run's provider response.
_STEP_METADATA = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}T\S+Z )?"
    r"(?:  +[A-Za-z_][A-Za-z0-9_-]*: [^\n]*|(?:env|with|shell):(?: [^\n]*)?)$"
)


_PROSE_PROVIDER_CLASSES = frozenset({"provider_throttle", "provider_quota", "model_unavailable"})
_EXIT = re.compile(r"\[factory:(?P<worker>\d+)\](?:\[[^\]\n]+\])*\s+agent exit status (?P<status>\d+) for ")
_ZERO_EXIT_REFUSAL = "agent exited 0 after a provider account refusal"


def _clean_final_exit(execution_log: str, worker: str) -> bool:
    """True when this worker's last agent session exited 0 and was not refused."""
    exits = [m for m in _EXIT.finditer(execution_log) if m["worker"] == worker]
    if not exits or exits[-1]["status"] != "0":
        return False
    return _ZERO_EXIT_REFUSAL not in execution_log[exits[-1].end():]


def strip_step_metadata(log: str) -> str:
    """Drop echoed step metadata lines before any evidence matching."""
    return "\n".join(
        line for line in log.splitlines() if not _STEP_METADATA.match(line)
    ) + ("\n" if log.endswith("\n") else "")


def _canonical() -> Any:
    spec = importlib.util.spec_from_file_location("factory_canonical_log_classifier", _CLASSIFIER)
    if spec is None or spec.loader is None:
        raise RuntimeError("canonical factory log classifier unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def classify(log: str) -> dict[str, Any]:
    canonical = _canonical()
    # The runtime model is recovered from the raw log; evidence never is.
    runtime_env = re.search(r"(?m)^.*\bFACTORY_RUNTIME_MODEL:\s*(\S+)", log)
    log = strip_step_metadata(log)
    # The old marker still exists for historical and explicitly pinned jobs.
    if canonical.LOCK_RE.search(log):
        return asdict(canonical.classify(log))
    match = _SLOT.search(log)
    if match is None:
        return asdict(canonical.classify(log))
    worker, source = match["worker"], match["source"]
    configured = match["configured"].lower() == "true"
    # A trusted worker can rotate sources inside one lease. Its terminal
    # classification belongs to the last executed provider/model, never the
    # dispatch-time source or an earlier candidate's log.
    boundary = log.rfind("FACTORY_EXECUTION_CANDIDATE: source=")
    execution_log = log[boundary:] if boundary >= 0 else log
    # The worker logs "checked out <kind> #N" once, *before* the first
    # FACTORY_EXECUTION_CANDIDATE marker. Without it the canonical classifier
    # loses the target: exact-model proof, delivered-PR detection and the
    # per-PR exact-head retry backoff all silently disappear (#5275 regression).
    if boundary >= 0 and not canonical.TARGET_RE.search(execution_log):
        marker = f"[factory:{worker}]"
        target_line = next(
            (line for line in reversed(log[:boundary].splitlines())
             if marker in line and canonical.TARGET_RE.search(line)),
            "",
        )
        if target_line:
            execution_log = target_line + "\n" + execution_log
    model_event = next(
        (event for event in reversed(list(_MODEL.finditer(execution_log)))
         if event["worker"] == worker),
        None,
    )
    if model_event is None:
        if _COOLING.search(log):
            return asdict(canonical.Result(
                worker=worker, source=source, configured=configured,
                outcome="HEALTHY / IDLE", outcome_class="no_work",
                detail="provider catalog has no candidates outside existing cooldown",
            ))
        return asdict(canonical.Result(
            worker=worker, source=source, configured=configured,
            outcome="CONTROL PLANE FAILURE", outcome_class="control_plane_failure",
            detail="dynamic provider slot failed before selecting a model",
        ))
    model = model_event["model"]
    source = model_event["source"]
    runtime_match = re.search(r"FACTORY_EXECUTION_CANDIDATE: source=\S+ model=\S+ runtime=(\S+)", execution_log)
    if runtime_match is None:
        runtime_match = runtime_env
    runtime = runtime_match[1] if runtime_match else f"{source}/{model}"
    lock = (
        f"Factory {worker} locked: source={source} model={model} "
        f"runtime={runtime} minute=:00 scheduler=dynamic configured={str(configured).lower()}\n"
    )
    result = canonical.classify(lock + execution_log)
    if result.outcome_class in _PROSE_PROVIDER_CLASSES and _clean_final_exit(execution_log, worker):
        # The final model session exited 0, so it was served. Quota, throttle
        # or missing-model words in its plain-text output are what the agent
        # read or wrote (docs mention quotas and HTTP 429), not a provider
        # response. Classify from the remaining evidence instead of cooling a
        # working model (run 44093346708: a completed review became a throttle).
        filtered = "\n".join(
            line for line in execution_log.splitlines()
            if not any(regex.search(line) for regex in (
                canonical.QUOTA_RE, canonical.RATE_LIMIT_RE, canonical.MODEL_MISSING_RE))
        )
        result = canonical.classify(lock + filtered)
    # Account-wide refusal is different from one inaccessible model. Do not
    # turn a failed model into a provider-wide outage or rate limit.
    executed_lines = "\n".join(
        line for line in execution_log.splitlines() if "\x1b[36;1m" not in line
    )
    if _ACCOUNT_CREDITS.search(executed_lines):
        result = replace(result, outcome="PROVIDER QUOTA", outcome_class="provider_quota",
                         detail="provider account usage credits or key budget exhausted")
    elif _MODEL_ACCESS.search(executed_lines):
        result = replace(result, outcome="MODEL MISSING", outcome_class="model_unavailable",
                         detail="provider account tier denied this model; other models remain eligible")
    elif (
        result.outcome_class in {"no_work", "unknown_failure"}
        and result.selected_kind == "pr"
        and result.delivery_outcome == "none"
        and any(
            m["pr"] == result.selected_number and f"[factory:{worker}]" in
            executed_lines[executed_lines.rfind("\n", 0, m.start()) + 1:m.start()]
            for m in _MISSING_VERDICT.finditer(executed_lines)
        )
    ):
        result = replace(result, outcome="WORK FAILURE", outcome_class="work_failure",
                         detail="semantic review ended without an authoritative terminal verdict")
    return asdict(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(classify(args.log.read_text(encoding="utf-8", errors="replace")),
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
