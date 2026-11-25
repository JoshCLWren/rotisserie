"""The OpenCode plugin that stops retries of quota refusals."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import factory_quota_reset as reset
import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".github/scripts/opencode-factory-quota-retry.mjs"
DAILY = "Rate limit exceeded: free-models-per-day-high-balance. "
RESET_HEADERS = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1763769600000"}
DAILY_BODY = json.dumps(
    {"error": {"message": DAILY, "code": 429, "metadata": {"headers": RESET_HEADERS}}}
)
HOOK_SCRIPT = """
const hooks = {}
await plugin.default.setup({ session: { hook: async (name, cb) => { hooks[name] = cb } } })
const decision = () => ({ retry: true, delay: 2000 })
const event = { attempt: 2, decision: decision(), error: ERROR }
hooks.retry(event)
const broken = { attempt: 2, decision: decision(), get error() { throw new Error("x") } }
hooks.retry(broken)
await plugin.default.setup({ session: { hook: async () => { throw new Error("no hook") } } })
console.log(JSON.stringify({ event: event.decision, broken: broken.decision }))
"""


def rate_limit(message: str, *, body: str = "", retry_after: int | None = None) -> dict:
    error: dict[str, object] = {"type": "provider.rate-limit", "message": message, "status": 429}
    if body:
        error["response"] = {"body": body}
    if retry_after is not None:
        error["retryAfterMs"] = retry_after
    return error


def node(script: str) -> object:
    driver = f"const plugin = await import({json.dumps(PLUGIN.as_uri())});\n{script}"
    result = subprocess.run(
        ["node", "--input-type=module", "-e", driver],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def event(error: dict, *, attempt: int = 2, delay: int = 2000, retry: bool = True) -> dict:
    decision = {"retry": True, "delay": delay} if retry else {"retry": False}
    return {"attempt": attempt, "error": error, "decision": decision}


def decide(error: dict, **kwargs: int | bool) -> dict | None:
    payload = json.dumps(event(error, **kwargs))  # type: ignore[arg-type]
    verdict = node(f"console.log(JSON.stringify(plugin.retryDecision({payload}) ?? null))")
    return verdict["decision"] if isinstance(verdict, dict) else None


def test_patterns_match_the_quota_reset_classifier() -> None:
    patterns = node("console.log(JSON.stringify(plugin.QUOTA_PATTERNS))")
    assert patterns == {
        "credits": reset.CREDITS_RE.pattern,
        "daily": reset.DAILY_RE.pattern,
        "monthly": reset.MONTHLY_RE.pattern,
    }
    assert all(rx.flags & re.I for rx in (reset.CREDITS_RE, reset.DAILY_RE, reset.MONTHLY_RE))


@pytest.mark.parametrize(
    "error",
    [
        rate_limit(DAILY, body=DAILY_BODY),
        rate_limit(DAILY, retry_after=2000),
        rate_limit("You exceeded your monthly usage limit"),
        rate_limit("Limit reached (RPD)", retry_after=1000),
        {"type": "provider.unknown", "message": "insufficient balance or no resource package"},
        {"type": "provider.quota", "message": "quota"},
        {"type": "provider.invalid-request", "message": "Payment required", "status": 402},
    ],
)
def test_quota_refusals_never_retry(error: dict) -> None:
    assert decide(error) == {"retry": False}


def test_short_throttle_keeps_one_retry_honouring_retry_after() -> None:
    error = rate_limit("Rate limit exceeded: free-models-per-min. ", retry_after=5000)
    assert decide(error, delay=5000) == {"retry": True, "delay": 5000}
    assert decide(error, attempt=3, delay=5000) == {"retry": False}


def test_throttle_delay_is_capped_and_long_retry_after_fails_now() -> None:
    assert decide(rate_limit("Too many requests"), delay=45000) == {"retry": True, "delay": 30000}
    long_wait = rate_limit("Too many requests", retry_after=60000)
    assert decide(long_wait, delay=60000) == {"retry": False}


def test_other_failures_keep_opencode_decision() -> None:
    assert decide({"type": "provider.internal", "message": "overloaded", "status": 503}) is None
    assert decide({"type": "provider.transport", "message": "ECONNRESET"}) is None
    assert decide(rate_limit("Too many requests"), retry=False) is None


def test_plugin_hook_sets_decision_and_fails_open() -> None:
    script = HOOK_SCRIPT.replace("ERROR", json.dumps(rate_limit(DAILY, body=DAILY_BODY)))
    assert node(script) == {"event": {"retry": False}, "broken": {"retry": True, "delay": 2000}}


def test_refusal_line_keeps_classifier_text_and_reset_hints() -> None:
    payload = json.dumps(event(rate_limit(DAILY, body=DAILY_BODY, retry_after=3000)))
    line = node(
        f"const e = {payload};\n"
        "console.log(JSON.stringify(plugin.refusalLine(e, plugin.retryDecision(e))))"
    )
    assert isinstance(line, str)
    assert "no retry (quota kind=daily)" in line
    assert "retry_after_ms=3000 x_ratelimit_reset=1763769600000" in line
    assert reset.DAILY_RE.search(line)
