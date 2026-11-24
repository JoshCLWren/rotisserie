#!/usr/bin/env python3
"""When a provider refusal lets the factory try again.

A quota, throttle or empty-balance refusal is provider capacity, not a model
fault, so it never grows a Fibonacci streak. Each one sets one cooldown that
ends when the capacity returns:

- daily quota: the provider's next daily reset (next UTC midnight unless the
  provider documents another time);
- monthly quota: the first instant of the next UTC month;
- empty credit balance: a recheck after NEEDS_CREDITS_SECONDS, for the paid
  models of that source only;
- throttle: the provider's own wait hint, else THROTTLE_DEFAULT_SECONDS.

A hint from the provider (refusal text, a Retry-After or X-RateLimit-Reset
header, or the OpenRouter key endpoint) wins over the policy default, clamped
to [now, now + MAX_RESET_SECONDS]. An oversized request (input larger than a
per-minute token limit) is not a quota and sets no cooldown.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DAILY, MONTHLY, NEEDS_CREDITS, THROTTLE, OVERSIZE = (
    "daily",
    "monthly",
    "needs_credits",
    "throttle",
    "oversize",
)
KINDS = frozenset({DAILY, MONTHLY, NEEDS_CREDITS, THROTTLE, OVERSIZE})
CAPACITY_OUTCOMES = frozenset({"provider_quota", "provider_throttle"})
RESET_SOURCES = frozenset({"text", "header", "key_endpoint", "policy"})
THROTTLE_DEFAULT_SECONDS = 300
THROTTLE_MAX_SECONDS = 3600
NEEDS_CREDITS_SECONDS = 24 * 3600
MAX_RESET_SECONDS = 32 * 24 * 3600


@dataclass(frozen=True)
class ResetPolicy:
    """How one source's quota resets and what a refusal blocks."""

    period: str
    scope: str
    probe: str
    timezone: str = "UTC"


# scope "account": one refusal blocks the whole source until reset.
# scope "model": limits are per model, so only the refused pair cools.
# probe "key_endpoint" reads remaining requests without inference; "inference"
# sends one 1-token request; "none" means the request headers OpenCode sends
# are not known, so the factory never probes and uses the policy default.
DEFAULT_POLICY = ResetPolicy(DAILY, "account", "none")
RESET_POLICIES: dict[str, ResetPolicy] = {
    # openrouter.ai/docs/api-reference/limits: free-model requests count per UTC day.
    "openrouter-free": ResetPolicy(DAILY, "account", "key_endpoint"),
    # Groq and Gemini publish per-model requests-per-day limits.
    "groq-free": ResetPolicy(DAILY, "model", "inference"),
    # ai.google.dev/gemini-api/docs/rate-limits: daily quotas reset at midnight Pacific.
    "gemini-free": ResetPolicy(DAILY, "model", "inference", "America/Los_Angeles"),
    "mistral": ResetPolicy(MONTHLY, "account", "inference"),
    "ollama-cloud": ResetPolicy(MONTHLY, "account", "inference"),
    "nvidia": ResetPolicy(DAILY, "account", "inference"),
    "z-ai": ResetPolicy(DAILY, "account", "inference"),
    "vercel-ai-gateway": ResetPolicy(DAILY, "account", "inference"),
    "pollinations": ResetPolicy(DAILY, "account", "inference"),
    "orcarouter": ResetPolicy(DAILY, "account", "inference"),
    "aion-free": ResetPolicy(DAILY, "account", "inference"),
    "cohere-eval": ResetPolicy(MONTHLY, "account", "inference"),
    "typhoon-free": ResetPolicy(DAILY, "account", "inference"),
    "cloudflare-workers-ai": ResetPolicy(DAILY, "account", "inference"),
    "opencode-free": DEFAULT_POLICY,
    "kilo-auto": DEFAULT_POLICY,
    "omniroute-free": DEFAULT_POLICY,
}

CREDITS_RE = re.compile(
    r"insufficient balance|no resource package|please recharge|add usage credits|"
    r"out of credits|balance is too low|credits? (?:depleted|exhausted)",
    re.I,
)
OVERSIZE_RE = re.compile(
    r"per minute \((?:ITPM|TPM)\): Limit (\d+), (?:Used \d+, )?Requested (\d+)",
    re.I,
)
DAILY_RE = re.compile(r"per[ -]day|\(RPD\)|free-models-per-day|daily (?:limit|quota)", re.I)
MONTHLY_RE = re.compile(r"monthly (?:usage )?limit|per month", re.I)
WAIT_RE = re.compile(
    r"(?:try again|retry) in\s+(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:(\d+(?:\.\d+)?)s)?",
    re.I,
)


def policy(source: str) -> ResetPolicy:
    return RESET_POLICIES.get(source, DEFAULT_POLICY)


def next_daily_reset(observed: datetime, zone: str = "UTC") -> datetime:
    local = observed.astimezone(ZoneInfo(zone))
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


def next_monthly_reset(observed: datetime) -> datetime:
    observed = observed.astimezone(UTC)
    year = observed.year + (observed.month == 12)
    return datetime(year, observed.month % 12 + 1, 1, tzinfo=UTC)


def policy_reset(source: str, kind: str, observed: datetime) -> datetime:
    """The documented reset for one refusal kind, without a provider hint."""
    rule = policy(source)
    if kind == THROTTLE:
        return observed + timedelta(seconds=THROTTLE_DEFAULT_SECONDS)
    if kind == NEEDS_CREDITS:
        return observed + timedelta(seconds=NEEDS_CREDITS_SECONDS)
    if kind == MONTHLY or (kind != DAILY and rule.period == MONTHLY):
        return next_monthly_reset(observed)
    return next_daily_reset(observed, rule.timezone)


def clamp(observed: datetime, reset: datetime, kind: str) -> datetime:
    ceiling = THROTTLE_MAX_SECONDS if kind == THROTTLE else MAX_RESET_SECONDS
    return min(max(reset, observed), observed + timedelta(seconds=ceiling))


def wait_seconds(text: str) -> float | None:
    """Seconds from "try again in 1m26.4s"-style refusal text."""
    for match in WAIT_RE.finditer(text):
        hours, minutes, seconds = match.groups()
        if hours is None and minutes is None and seconds is None:
            continue
        return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)
    return None


def refusal_kind(outcome: str, text: str) -> str:
    """Which capacity the refusal text says ran out."""
    oversize = OVERSIZE_RE.search(text)
    if oversize and int(oversize.group(2)) > int(oversize.group(1)):
        return OVERSIZE
    if CREDITS_RE.search(text):
        return NEEDS_CREDITS
    if outcome == "provider_throttle" and not DAILY_RE.search(text):
        return THROTTLE
    if MONTHLY_RE.search(text):
        return MONTHLY
    if DAILY_RE.search(text):
        return DAILY
    return THROTTLE if outcome == "provider_throttle" else ""


@dataclass(frozen=True)
class Reset:
    kind: str
    until: datetime | None
    reset_source: str

    def fields(self) -> str:
        epoch = int(self.until.timestamp()) if self.until else 0
        return f"kind={self.kind} reset_at={epoch} reset_source={self.reset_source}"


def from_text(source: str, outcome: str, text: str, observed: datetime) -> Reset:
    """The cooldown a refusal's own text supports, else the policy default."""
    kind = refusal_kind(outcome, text) or policy(source).period
    if kind == OVERSIZE:
        return Reset(OVERSIZE, None, "text")
    wait = wait_seconds(text)
    if wait is not None and kind in {THROTTLE, DAILY}:
        return Reset(kind, clamp(observed, observed + timedelta(seconds=wait), kind), "text")
    return Reset(kind, policy_reset(source, kind, observed), "policy")


def resolve(source: str, outcome: str, hint: dict[str, object] | None, observed: datetime) -> Reset:
    """Trusted cooldown for one capacity refusal from a validated marker."""
    if hint:
        kind = str(hint.get("kind") or "")
        reset_source = str(hint.get("reset_source") or "policy")
        epoch = hint.get("reset_at")
        if kind == OVERSIZE:
            return Reset(OVERSIZE, None, reset_source)
        if kind in KINDS and isinstance(epoch, int) and epoch > 0:
            until = datetime.fromtimestamp(epoch, UTC)
            return Reset(kind, clamp(observed, until, kind), reset_source)
        if kind in KINDS:
            return Reset(kind, policy_reset(source, kind, observed), "policy")
    kind = THROTTLE if outcome == "provider_throttle" else policy(source).period
    return Reset(kind, policy_reset(source, kind, observed), "policy")


def scope(source: str, kind: str) -> str:
    """account, model or pool: what this refusal blocks."""
    if kind == NEEDS_CREDITS:
        return "pool"
    return policy(source).scope


MARKER_RE = re.compile(
    r"FACTORY_QUOTA_RESET: source=(?P<source>[a-z][a-z0-9-]*) model=(?P<model>[\w./:-]{1,180}) "
    r"outcome=(?P<outcome>provider_quota|provider_throttle) kind=(?P<kind>[a-z_]+) "
    r"reset_at=(?P<reset_at>\d{1,12}) reset_source=(?P<reset_source>[a-z_]+)\s*$"
)


def parse_marker(message: str) -> dict[str, object] | None:
    match = MARKER_RE.search(message)
    if not match or match["kind"] not in KINDS or match["reset_source"] not in RESET_SOURCES:
        return None
    return {**match.groupdict(), "reset_at": int(match["reset_at"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("hint",))
    parser.add_argument("--source", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--outcome", required=True, choices=sorted(CAPACITY_OUTCOMES))
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--probe-cache", type=Path)
    args = parser.parse_args()
    observed = datetime.now(UTC)
    try:
        with args.log.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 65536))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        text = ""
    reset = from_text(args.source, args.outcome, text, observed)
    if args.probe and reset.reset_source == "policy" and reset.kind != NEEDS_CREDITS:
        from factory_quota_probe import probe_reset

        probed = probe_reset(args.source, args.model, reset.kind, observed, args.probe_cache)
        if probed is not None:
            reset = probed
    print(f"source={args.source} model={args.model} outcome={args.outcome} {reset.fields()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
