#!/usr/bin/env python3
"""Rotate the live, proven-free OpenCode catalog over durable factory identities."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from factory_candidate_health import (
    HEALTHY_OUTCOMES,
    OUTCOME_RE,
    RETIREMENT_MARKER,
    UPDATED_RE,
    flatten_comments,
    latest_attempt_evidence,
)
from factory_completion_controller import parse_time
from factory_model_catalog import parse_opencode_models_text

COOLDOWN_SECONDS = 6 * 60 * 60
MODEL_UNAVAILABLE_RE = re.compile(
    r"Error:\s*Model unavailable:\s*(?P<model>\S+)",
    re.IGNORECASE,
)
MODEL_DEPRECATED_RE = re.compile(
    r"\bModel\s+(?P<model>[A-Za-z0-9][\w./:-]*)\s+has\s+been\s+deprecated\b",
    re.IGNORECASE,
)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
RUN_RE = re.compile(r"(?m)^Run: ([0-9]+)$")
FREE_QUOTA_RE = re.compile(r"Free usage limit reached|FreeUsageLimitError", re.IGNORECASE)


def catalog_models(raw_catalog: str, *, provider: str = "opencode-free") -> list[str]:
    """Enumerate current provider models without any name/price allowlist."""
    if provider not in {"opencode-free", "openrouter-free"}:
        raise ValueError("unsupported OpenCode catalog provider")
    catalog = parse_opencode_models_text(raw_catalog, provider.removesuffix("-free"))
    return sorted(
        entry.model_id for entry in catalog.entries
        if provider != "openrouter-free" or entry.model_id.endswith(":free")
    )


def unavailable_models(
    payload: object, *, now_epoch: int, provider: str = "opencode-free"
) -> set[str]:
    """Return model-specific exclusions from trusted, timestamped registry records."""
    comments = []
    for comment in flatten_comments(payload):
        body = str(comment.get("body") or "")
        if RUN_RE.search(body) is None:
            continue
        if not any(
            marker in body
            for marker in (
                "<!-- factory-attempt-outcome:v1 ",
                "<!-- factory-model-unavailable:v1 ",
                "<!-- factory-provider-quota:v1 ",
                "<!-- factory-model-retired-410:v1 -->",
            )
        ):
            continue
        timestamp = UPDATED_RE.search(body)
        updated = parse_time(timestamp.group("updated")) if timestamp else None
        if updated is None or updated > now_epoch:
            continue
        # Generic/provider/control-plane failures do not prove model recovery.
        # Do not let their newer worker summary hide durable unavailability.
        outcome = OUTCOME_RE.search(body)
        if RETIREMENT_MARKER not in body and (
            outcome is None
            or outcome.group("outcome")
            not in HEALTHY_OUTCOMES | {"model_unavailable", "model_retired_410", "provider_quota"}
        ):
            continue
        comments.append(comment)
    excluded = set()
    for evidence in latest_attempt_evidence(comments).values():
        if evidence.provider != provider or evidence.updated > now_epoch:
            continue
        if evidence.outcome == "model_retired_410" or (
            evidence.outcome in {"model_unavailable", "provider_quota"}
            and now_epoch < evidence.updated + COOLDOWN_SECONDS
        ):
            excluded.add(evidence.model.removeprefix(provider.removesuffix("-free") + "/"))
    return excluded


def record_smoke_failure(log_content: str, runtime_model: str, outcome_file: Path) -> bool:
    """Persist recognizable OpenCode smoke errors for the registry reporter."""
    # OpenCode colorizes errors; ANSI sequences can hide word boundaries.
    log_content = ANSI_ESCAPE_RE.sub("", log_content)
    if re.search(
        r"budget[ _]exhausted|billing_api_budget_exhausted|payment required|HTTP[^0-9]*402|"
        r"(monthly|daily|weekly|free) usage limit|reached your [a-z ]*usage limit|"
        r"insufficient[ _](credits|balance|quota)|exceeded your current quota|"
        r"quota[ _](exceeded|exhausted)|free-models-per-day|"
        r"rate limit exceeded: free-models-per-day",
        log_content,
        re.I,
    ):
        outcome_file.write_text(
            f"provider_quota\tProvider account quota or budget exhausted for {runtime_model}\n",
            encoding="utf-8",
        )
        return True
    if re.search(
        r"(?<![0-9.])(429|413)(?!\d)|too many requests|request too large|rate.?limit|"
        r"\bquota\b|throttl|capacity[^\n]*(exceeded|exhausted|unavailable)|"
        r"(exceeded|exhausted|unavailable)[^\n]*capacity|tokens per minute|\btpm\b",
        log_content,
        re.I,
    ):
        outcome_file.write_text(
            f"provider_throttle\tProvider throttled or rate-limited {runtime_model}\n",
            encoding="utf-8",
        )
        return True
    # A deprecated model may still appear in OpenCode's live catalog.
    # Recognize only the exact requested selector, otherwise a different
    # model mentioned in output could incorrectly poison the current one.
    selector = runtime_model.split("/", 1)[-1]
    if any(
        match.group("model") in {runtime_model, selector}
        for match in MODEL_DEPRECATED_RE.finditer(log_content)
    ):
        outcome_file.write_text(
            f"model_unavailable\tModel deprecated: {runtime_model}\n",
            encoding="utf-8",
        )
        return True
    if not any(
        match.group("model") == runtime_model
        for match in MODEL_UNAVAILABLE_RE.finditer(log_content)
    ):
        return False
    outcome_file.write_text(
        f"model_unavailable\tModel unavailable: {runtime_model}\n",
        encoding="utf-8",
    )
    return True


def select_model(
    raw_catalog: str,
    worker: int,
    rotation: int,
    roster_path: Path | None = None,
    comments_path: Path | None = None,
    exclusions_path: Path | None = None,
    *,
    now_epoch: int | None = None,
    attempted: set[str] | None = None,
    provider: str = "opencode-free",
) -> dict[str, str | int]:
    """Rotate live models across stable worker identities without a model roster.

    A catalog entry is a candidate, not proof of useful execution.
    Trusted runtime failures and cooldowns remain authoritative.
    """
    del roster_path  # Backward-compatible call signature during roster removal.
    models = catalog_models(raw_catalog, provider=provider)
    if not models:
        raise ValueError("OpenCode catalog has no candidates")

    excluded = set()
    if comments_path is not None:
        # Evidence read/JSON errors must fail closed rather than silently forget cooldowns.
        payload = json.loads(comments_path.read_text(encoding="utf-8"))
        excluded = unavailable_models(
            payload,
            now_epoch=int(time.time()) if now_epoch is None else now_epoch,
            provider=provider,
        )
    if exclusions_path is not None:
        # Supplied by the trusted Entry job. Workers never receive the DSN.
        snapshot = json.loads(exclusions_path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or any(
            not isinstance(values, list) or not all(isinstance(v, str) for v in values)
            for values in snapshot.values()
        ):
            raise ValueError("invalid trusted Neon model exclusions")
        excluded.update(snapshot.get(provider, []))
    available_models = [model for model in models if model not in excluded and model not in (attempted or set())]
    if not available_models:
        raise ValueError(f"All {len(models)} candidates are unavailable or in cooldown")

    if worker < 1:
        raise ValueError("worker identity must be a positive integer")
    model = available_models[(worker - 1 + rotation) % len(available_models)]
    return {
        "model": model,
        "runtime_model": f"{provider.removesuffix('-free')}/{model}",
        "provider": provider,
        "candidate_count": len(models),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--comments", type=Path)
    parser.add_argument("--exclusions-file", type=Path)
    parser.add_argument("--smoke-log", type=Path)
    parser.add_argument("--runtime-model")
    parser.add_argument("--outcome-file", type=Path)
    parser.add_argument("--worker", type=int)
    parser.add_argument("--rotation", type=int, default=0)
    parser.add_argument("--attempted", action="append", default=[])
    parser.add_argument("--provider", choices=("opencode-free", "openrouter-free"), default="opencode-free")
    args = parser.parse_args()
    if args.smoke_log is not None:
        if not args.runtime_model or args.outcome_file is None:
            parser.error("--smoke-log requires --runtime-model and --outcome-file")
        record_smoke_failure(
            args.smoke_log.read_text(encoding="utf-8"),
            args.runtime_model,
            args.outcome_file,
        )
        return
    if args.catalog is None or args.worker is None:
        parser.error("selection requires --catalog and --worker")
    selected = select_model(
        args.catalog.read_text(encoding="utf-8"),
        args.worker,
        args.rotation,
        comments_path=args.comments,
        exclusions_path=args.exclusions_file,
        attempted=set(args.attempted),
        provider=args.provider,
    )
    print(json.dumps(selected, sort_keys=True))


if __name__ == "__main__":
    main()
