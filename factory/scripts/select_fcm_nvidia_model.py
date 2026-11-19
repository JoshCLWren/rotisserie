#!/usr/bin/env python3
"""Select a healthy NVIDIA chat model from free-coding-models JSON output."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass


EXCLUDED_MODEL_TERMS = (
    "embed",
    "image",
    "omni",
    "rerank",
    "safety",
    "vision",
)
TIER_RANK = {"S+": 4, "S": 3, "A+": 2, "A": 1}
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass(frozen=True)
class Candidate:
    """A healthy NVIDIA model that OpenCode can invoke."""

    model_id: str
    tier: str
    stability: float
    uptime: float
    latency: float

    @property
    def opencode_model(self) -> str:
        """Return the provider-qualified OpenCode model identifier."""
        return f"nvidia/{self.model_id}"

    @property
    def sort_key(self) -> tuple[float, float, float, float, str]:
        """Return a deterministic best-first ranking key."""
        return (
            -float(TIER_RANK.get(self.tier, 0)),
            -self.stability,
            -self.uptime,
            self.latency,
            self.model_id,
        )


def _number(value: object, default: float) -> float:
    """Convert a JSON number to float without accepting booleans or strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _candidate(value: object) -> Candidate | None:
    """Convert one FCM result into a candidate when it is safe to select."""
    if not isinstance(value, dict):
        return None

    provider = value.get("provider")
    status = value.get("status")
    model_id = value.get("modelId")
    tier = value.get("tier")
    if provider != "nvidia" or status != "up":
        return None
    if not isinstance(model_id, str) or not isinstance(tier, str):
        return None
    if not MODEL_ID_PATTERN.fullmatch(model_id):
        return None

    normalized_id = model_id.lower()
    if any(term in normalized_id for term in EXCLUDED_MODEL_TERMS):
        return None
    if tier not in TIER_RANK:
        return None

    return Candidate(
        model_id=model_id,
        tier=tier,
        stability=_number(value.get("stability"), 0),
        uptime=_number(value.get("uptime"), 0),
        latency=_number(value.get("latestPing"), float("inf")),
    )


def select_models(raw_output: str) -> list[str]:
    """Rank NVIDIA models from possibly banner-prefixed FCM output.

    Args:
        raw_output: Complete stdout emitted by ``free-coding-models --json``.

    Returns:
        Provider-qualified model identifiers ordered best-first for OpenCode.

    Raises:
        ValueError: When the output is malformed or has no healthy candidate.
    """
    payload_start = raw_output.find("[")
    if payload_start < 0:
        raise ValueError("FCM output did not contain a JSON array")

    payload: object = json.loads(raw_output[payload_start:])
    if not isinstance(payload, list):
        raise ValueError("FCM JSON payload was not an array")

    candidates = [candidate for item in payload if (candidate := _candidate(item))]
    if not candidates:
        raise ValueError("FCM found no healthy S- or A-tier NVIDIA chat model")
    return [candidate.opencode_model for candidate in sorted(candidates, key=lambda item: item.sort_key)]


def select_model(raw_output: str) -> str:
    """Return the highest-ranked NVIDIA model from FCM output.

    Args:
        raw_output: Complete stdout emitted by ``free-coding-models --json``.

    Returns:
        The first provider-qualified model identifier.
    """
    return select_models(raw_output)[0]


def main() -> int:
    """Read FCM output from stdin and print one selected OpenCode model."""
    try:
        print("\n".join(select_models(sys.stdin.read())))
    except (json.JSONDecodeError, ValueError) as error:
        print(f"Unable to select NVIDIA model: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
