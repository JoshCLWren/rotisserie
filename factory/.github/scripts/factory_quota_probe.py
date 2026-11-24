#!/usr/bin/env python3
"""Read a provider's real reset time once after a quota or throttle refusal.

OpenCode hides the refusal headers, so after a capacity refusal the trusted
worker step may ask the provider once. OpenRouter answers from its key
endpoint without inference. Other providers in factory_provider_registry get
one 1-token request with the endpoint, model and extra headers OpenCode uses.
A source whose request headers are not known is never probed.

Only parsed numbers leave this module: no key, header block, URL or exception
text is printed. A 401 or 403 is a blocked probe, never a reset signal. The
first answer per source is cached in the run's probe file, so later refusals
in the same window reuse it instead of spending another request.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from factory_quota_reset import DAILY, THROTTLE, Reset, clamp, next_daily_reset, policy

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
# OpenCode adds these to NVIDIA requests (packages/core/src/plugin/provider/nvidia.ts).
# Its per-session x-opencode-* headers carry no credential and are not replicated.
OPENCODE_HEADERS = {"nvidia": {"HTTP-Referer": "https://opencode.ai/", "X-Title": "opencode"}}
USER_AGENT = "rotisserie-factory-quota-probe/1"
TIMEOUT = 15.0

Opener = Callable[[urllib.request.Request], Any]


def _open(request: urllib.request.Request) -> Any:
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def reset_from_headers(headers: Mapping[str, str], observed: datetime) -> datetime | None:
    """Retry-After (seconds or HTTP date) or X-RateLimit-Reset (ms, s or delta)."""
    lowered = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    retry_after = lowered.get("retry-after", "")
    if retry_after:
        try:
            return observed + timedelta(seconds=float(retry_after))
        except ValueError:
            try:
                return parsedate_to_datetime(retry_after).astimezone(UTC)
            except TypeError, ValueError:
                pass
    for name in ("x-ratelimit-reset", "x-ratelimit-reset-requests"):
        value = lowered.get(name, "")
        if not value:
            continue
        try:
            number = float(value)
        except ValueError:
            from factory_quota_reset import wait_seconds

            seconds = wait_seconds(f"try again in {value}")
            if seconds is not None:
                return observed + timedelta(seconds=seconds)
            continue
        if number > 1e12:
            return datetime.fromtimestamp(number / 1000, UTC)
        if number > 1e9:
            return datetime.fromtimestamp(number, UTC)
        return observed + timedelta(seconds=number)
    return None


def _openrouter(env: Mapping[str, str], observed: datetime, opener: Opener) -> Reset | None:
    key = env.get("OPENROUTER_API_KEY", "")
    if not key:
        return None
    request = urllib.request.Request(
        OPENROUTER_KEY_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with opener(request) as response:
            body = json.loads(response.read(100_000))
    except OSError, urllib.error.URLError, ValueError:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    usage = data.get("free_model_daily_requests") if isinstance(data, dict) else None
    remaining = usage.get("remaining") if isinstance(usage, dict) else None
    if isinstance(remaining, int) and not isinstance(remaining, bool) and remaining <= 0:
        return Reset(DAILY, next_daily_reset(observed), "key_endpoint")
    return None


def _inference(
    source: str, model: str, kind: str, env: Mapping[str, str], observed: datetime, opener: Opener
) -> Reset | None:
    from factory_provider_registry import PROVIDERS, missing_credentials

    spec = PROVIDERS.get(source)
    if spec is None or missing_credentials(spec, env):
        return None
    endpoint = spec.endpoint
    if spec.additional_credentials:
        endpoint = endpoint.replace("{account_id}", env["CLOUDFLARE_ACCOUNT_ID"])
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        }
    ).encode()
    headers = {
        "Authorization": f"Bearer {env[spec.credential]}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        **dict(spec.headers),
        **OPENCODE_HEADERS.get(source, {}),
    }
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with opener(request):
            return None
    except urllib.error.HTTPError as refusal:
        if refusal.code != 429:
            return None
        until = reset_from_headers(dict(refusal.headers or {}), observed)
    except OSError, urllib.error.URLError, ValueError:
        return None
    if until is None:
        return None
    return Reset(kind, clamp(observed, until, kind), "header")


def _cached(cache: Path | None, source: str, observed: datetime) -> Reset | None | bool:
    if cache is None or not cache.is_file():
        return False
    try:
        entry = json.loads(cache.read_text(encoding="utf-8")).get(source)
    except OSError, ValueError, AttributeError:
        return False
    if not isinstance(entry, dict):
        return False
    until = int(entry.get("reset_at") or 0)
    if entry.get("probed_at", 0) + 24 * 3600 < observed.timestamp():
        return False
    if not until:
        return None
    return Reset(
        str(entry.get("kind") or THROTTLE),
        datetime.fromtimestamp(until, UTC),
        str(entry.get("reset_source") or "header"),
    )


def _remember(cache: Path | None, source: str, observed: datetime, reset: Reset | None) -> None:
    if cache is None:
        return
    try:
        state = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
    except OSError, ValueError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    state[source] = {
        "probed_at": int(observed.timestamp()),
        "reset_at": int(reset.until.timestamp()) if reset and reset.until else 0,
        "kind": reset.kind if reset else "",
        "reset_source": reset.reset_source if reset else "",
    }
    cache.write_text(json.dumps(state), encoding="utf-8")


def probe_reset(
    source: str,
    model: str,
    kind: str,
    observed: datetime,
    cache: Path | None,
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> Reset | None:
    """At most one probe per source per run; None means use the policy default."""
    cached = _cached(cache, source, observed)
    if cached is not False:
        return cached if isinstance(cached, Reset) else None
    env = os.environ if env is None else env
    opener = opener or _open
    method = policy(source).probe
    if method == "key_endpoint":
        reset = _openrouter(env, observed, opener)
    elif method == "inference":
        reset = _inference(source, model, kind, env, observed, opener)
    else:
        return None
    _remember(cache, source, observed, reset)
    return reset
