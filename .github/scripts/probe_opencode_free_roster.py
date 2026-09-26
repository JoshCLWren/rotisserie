#!/usr/bin/env python3
"""Probe OmniRoute for the live OpenCode free-model roster.

The factory status dashboard uses this to show operators whether free
OpenCode / Zen models are currently usable. Collection is fail-soft: missing
credentials, catalog errors, or a dead gateway still produce a table with
``unknown`` / ``stale`` rows instead of aborting dashboard generation.

Free OpenCode roster models stay first-class. opencode and opencode-zen
connections can both be active. This module never treats Zen as disabled.
The opencode-zen *providerOverride* (blanket whole-provider=free) is what we
avoid for paid bleed — roster models stay eligible via catalog, pricing, and
freeProviders.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

OPENCODE_PREFIXES = ("oc/", "opencode/", "opencode-zen/")
CONNECTION_PROVIDERS = ("opencode", "opencode-zen")
ALWAYS_NAMED = {"big-pickle"}
MUSE_SPARK_RE = re.compile(r"muse-spark", re.I)
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "free-model-factories.tsv"
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_PROBE_LIMIT = 24
STATUS_OK = "ok"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_UNSUPPORTED = "unsupported"
STATUS_CATALOG_MISS = "catalog_miss"
STATUS_AUTH_PAID = "auth_paid"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"
MODEL_STATUSES = (
    STATUS_OK,
    STATUS_RATE_LIMITED,
    STATUS_UNSUPPORTED,
    STATUS_CATALOG_MISS,
    STATUS_AUTH_PAID,
    STATUS_TIMEOUT,
    STATUS_ERROR,
    STATUS_UNKNOWN,
)
HttpFn = Callable[..., "HttpResult"]


@dataclass(frozen=True)
class HttpResult:
    """One HTTP response or transport failure."""

    status_code: int | None = None
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    timed_out: bool = False
    error: str = ""


@dataclass(frozen=True)
class ConnectionStatus:
    """One OmniRoute provider connection we care about for the free roster."""

    provider: str
    active: bool | None
    test_status: str
    detail: str = ""


@dataclass(frozen=True)
class RosterModel:
    """One probed or catalog-derived free-roster model row."""

    model_id: str
    connection: str
    status: str
    detail: str
    http_status: int | None = None
    cost: str | None = None
    in_catalog: bool | None = None


@dataclass(frozen=True)
class RosterSnapshot:
    """Fail-soft snapshot rendered by the factory status dashboard."""

    checked_at: str
    freshness: str
    detail: str
    connections: tuple[ConnectionStatus, ...] = ()
    models: tuple[RosterModel, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot."""
        return {
            "checked_at": self.checked_at,
            "freshness": self.freshness,
            "detail": self.detail,
            "connections": [asdict(item) for item in self.connections],
            "models": [asdict(item) for item in self.models],
            "counts": dict(self.counts),
        }


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def gateway_root(base_url: str) -> str:
    """Strip a trailing ``/v1`` so management and proxy paths share one origin."""
    url = base_url.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url.rstrip("/")


def split_model_id(model_id: str) -> tuple[str, str]:
    """Split ``oc/name`` into a connection prefix and bare model name."""
    raw = model_id.strip()
    lowered = raw.lower()
    for prefix in OPENCODE_PREFIXES:
        if lowered.startswith(prefix):
            return prefix[:-1], raw[len(prefix) :]
    return "", raw


def is_free_roster_model(model_id: str) -> bool:
    """Return whether a model belongs on the OpenCode free-roster table.

    Args:
        model_id: Catalog, factory, or probe identifier.

    Returns:
        True when the id is an ``oc`` / ``opencode`` / ``opencode-zen`` (or
        bare factory) model that is free-ish: ``-free`` suffix, ``big-pickle``,
        or a muse-spark variant.
    """
    raw = model_id.strip()
    if not raw:
        return False
    connection, name = split_model_id(raw)
    if "/" in name:
        return False
    if connection and connection not in {item[:-1] for item in OPENCODE_PREFIXES}:
        return False
    lowered = name.lower()
    return (
        lowered in ALWAYS_NAMED
        or lowered.endswith("-free")
        or bool(MUSE_SPARK_RE.search(lowered))
    )


def load_manifest_free_models(path: Path) -> list[str]:
    """Return unique ``opencode-free`` model names from the factory TSV."""
    if not path.is_file():
        return []
    seen: set[str] = set()
    models: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in raw_line.split("\t")]
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        source, model = fields[1], fields[2]
        if source != "opencode-free" or not model or model in seen:
            continue
        if not is_free_roster_model(model):
            continue
        seen.add(model)
        models.append(model)
    return models


def _decimal(value: object) -> Decimal | None:
    """Parse a cost header or catalog price, failing closed."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def classify_probe_response(
    *,
    status_code: int | None,
    body: str = "",
    cost: object = None,
    timed_out: bool = False,
    error: str = "",
) -> tuple[str, str]:
    """Map one OmniRoute probe into a dashboard status bucket.

    Args:
        status_code: HTTP status if a response arrived.
        body: Response body or error text.
        cost: ``X-OmniRoute-Response-Cost`` or equivalent.
        timed_out: True when the transport timed out.
        error: Transport error without an HTTP status.

    Returns:
        ``(status, detail)`` for the roster table.
    """
    text = body or error
    lowered = text.lower()
    amount = _decimal(cost)

    if timed_out:
        return STATUS_TIMEOUT, "probe timed out"
    if status_code == 429 or "too many requests" in lowered or "cooling" in lowered:
        return STATUS_RATE_LIMITED, _brief_detail(status_code, text, fallback="429 / cooling")
    if status_code == 402 or "wants api key" in lowered or "payment required" in lowered:
        return STATUS_AUTH_PAID, _brief_detail(status_code, text, fallback="402 wants API key")
    if status_code == 401 and "not supported" in lowered:
        return STATUS_UNSUPPORTED, _brief_detail(status_code, text, fallback="401 not supported")
    if status_code == 400 and (
        "not available in the active live catalog" in lowered or "not available" in lowered
    ):
        return STATUS_CATALOG_MISS, _brief_detail(
            status_code, text, fallback="400 not available in the active live catalog"
        )
    if status_code == 200:
        if amount is not None and amount != 0:
            return STATUS_AUTH_PAID, f"200 with non-zero cost ${amount}"
        return STATUS_OK, "200, $0"
    if status_code is None:
        return STATUS_ERROR, _brief_detail(None, text, fallback=error or "transport error")
    return STATUS_ERROR, _brief_detail(status_code, text, fallback=f"HTTP {status_code}")


def _brief_detail(status_code: int | None, body: str, *, fallback: str) -> str:
    """Keep operator-facing detail to one short line."""
    compact = re.sub(r"\s+", " ", body).strip()
    if not compact:
        return fallback
    if status_code is not None and not compact.startswith(str(status_code)):
        compact = f"{status_code} {compact}"
    return compact[:180]


def extract_model_ids(payload: object) -> list[str]:
    """Walk a catalog or ``/v1/models`` payload for string model identifiers."""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        model = value.strip()
        if model and model not in seen:
            seen.add(model)
            found.append(model)

    def walk(node: object) -> None:
        if isinstance(node, str):
            if "/" in node or is_free_roster_model(node):
                add(node)
            return
        if isinstance(node, Mapping):
            identifier = node.get("id") or node.get("model") or node.get("name")
            if isinstance(identifier, str):
                add(identifier)
            for key, value in node.items():
                if key in {"id", "model", "name"}:
                    continue
                walk(value)
            return
        if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for item in node:
                walk(item)

    walk(payload)
    return found


def extract_connections(payload: object) -> list[ConnectionStatus]:
    """Parse ``/api/providers`` into opencode / opencode-zen rows."""
    items: list[object]
    if isinstance(payload, Mapping):
        raw = payload.get("connections") or payload.get("data") or payload.get("providers")
        items = list(raw) if isinstance(raw, list) else [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    by_provider: dict[str, ConnectionStatus] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        provider = str(item.get("provider") or item.get("id") or item.get("name") or "")
        lowered = provider.lower()
        if lowered not in CONNECTION_PROVIDERS:
            continue
        active_raw = item.get("isActive")
        if active_raw is None:
            active_raw = item.get("is_active")
        active = None if active_raw is None else bool(active_raw)
        test_status = str(item.get("testStatus") or item.get("test_status") or "untested")
        detail = "active" if active else "configured"
        if test_status and test_status != "untested":
            detail = f"{detail} · test {test_status}"
        by_provider[lowered] = ConnectionStatus(
            provider=lowered,
            active=active,
            test_status=test_status,
            detail=detail,
        )

    return [
        by_provider.get(
            name,
            ConnectionStatus(
                provider=name,
                active=None,
                test_status="unknown",
                detail="not returned by /api/providers",
            ),
        )
        for name in CONNECTION_PROVIDERS
    ]


def default_http(
    method: str,
    url: str,
    *,
    token: str,
    payload: Mapping[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> HttpResult:
    """Issue one bounded HTTP request using the standard library."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            header_map = {key.lower(): value for key, value in response.headers.items()}
            return HttpResult(status_code=int(response.status), body=body, headers=header_map)
    except TimeoutError:
        return HttpResult(timed_out=True, error="timeout")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        header_map = {key.lower(): value for key, value in (exc.headers.items() if exc.headers else [])}
        return HttpResult(status_code=int(exc.code), body=body, headers=header_map, error=str(exc))
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        timed_out = "timed out" in reason.lower()
        return HttpResult(timed_out=timed_out, error=reason)


def _json_body(result: HttpResult) -> object | None:
    """Decode JSON when present."""
    if not result.body.strip():
        return None
    try:
        return json.loads(result.body)
    except json.JSONDecodeError:
        return None


def _token(*names: str, env: Mapping[str, str]) -> str:
    """Return the first non-empty environment token."""
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return ""


def build_probe_ids(
    manifest_models: Sequence[str],
    catalog_ids: Sequence[str],
    *,
    limit: int = DEFAULT_PROBE_LIMIT,
) -> list[str]:
    """Union factory TSV models with catalog free-roster IDs.

    Factory names are expanded to both ``oc/`` and ``opencode-zen/`` so the
    table can show each live connection without implying Zen is unavailable.
    """
    ids: list[str] = []
    seen: set[str] = set()

    def add(model_id: str) -> None:
        value = model_id.strip()
        if not value or value in seen or not is_free_roster_model(value):
            return
        seen.add(value)
        ids.append(value)

    for name in manifest_models:
        connection, bare = split_model_id(name)
        if connection:
            add(name)
            continue
        add(f"oc/{bare}")
        add(f"opencode-zen/{bare}")
    for catalog_id in catalog_ids:
        add(catalog_id)
    return ids[:limit]


def count_statuses(models: Iterable[RosterModel]) -> dict[str, int]:
    """Count roster rows by status bucket."""
    counts = dict.fromkeys(MODEL_STATUSES, 0)
    for model in models:
        counts[model.status] = counts.get(model.status, 0) + 1
    return counts


def _unknown_models(manifest_models: Sequence[str], *, detail: str) -> tuple[RosterModel, ...]:
    """Build unknown rows from factory free-roster names."""
    return tuple(
        RosterModel(
            model_id=model_id,
            connection=split_model_id(model_id)[0] or "opencode",
            status=STATUS_UNKNOWN,
            detail=detail,
        )
        for model_id in build_probe_ids(manifest_models, ())
    )


def stale_snapshot(
    *,
    detail: str,
    models: Sequence[RosterModel] = (),
    connections: Sequence[ConnectionStatus] = (),
    checked_at: str | None = None,
    freshness: str = "stale",
) -> RosterSnapshot:
    """Build a table-ready snapshot when live probing cannot finish."""
    rows = tuple(models)
    if not rows:
        rows = tuple(
            RosterModel(
                model_id=model_id,
                connection=split_model_id(model_id)[0] or "opencode",
                status=STATUS_UNKNOWN,
                detail=detail,
            )
            for model_id in build_probe_ids(load_manifest_free_models(DEFAULT_MANIFEST), ())
        )
    return RosterSnapshot(
        checked_at=checked_at or utc_now(),
        freshness=freshness,
        detail=detail,
        connections=tuple(connections) or tuple(
            ConnectionStatus(
                provider=name,
                active=None,
                test_status="unknown",
                detail="connection status unavailable",
            )
            for name in CONNECTION_PROVIDERS
        ),
        models=rows,
        counts=count_statuses(rows),
    )


def collect_roster(
    *,
    env: Mapping[str, str] | None = None,
    manifest_path: Path | None = None,
    http: HttpFn | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    probe_limit: int = DEFAULT_PROBE_LIMIT,
    now: str | None = None,
) -> RosterSnapshot:
    """Collect a fail-soft OpenCode free-roster snapshot.

    Args:
        env: Process environment override for tests.
        manifest_path: Factory TSV used to seed known free models.
        http: Injectable transport. Defaults to :func:`default_http`.
        timeout: Per-request timeout in seconds.
        probe_limit: Maximum chat-completion probes in one collection.
        now: Frozen timestamp for tests.

    Returns:
        A snapshot that is always safe to render.
    """
    checked_at = now or utc_now()
    manifest = manifest_path or DEFAULT_MANIFEST
    try:
        return _collect_roster(
            env=env,
            manifest_path=manifest,
            http=http,
            timeout=timeout,
            probe_limit=probe_limit,
            now=checked_at,
        )
    except Exception as exc:
        return stale_snapshot(
            detail=(
                "OpenCode free-roster probe failed; table is unknown/stale. "
                f"{exc} Free OpenCode models remain eligible."
            ),
            models=_unknown_models(load_manifest_free_models(manifest), detail="probe failed"),
            checked_at=checked_at,
            freshness="stale",
        )


def _collect_roster(
    *,
    env: Mapping[str, str] | None,
    manifest_path: Path | None,
    http: HttpFn | None,
    timeout: float,
    probe_limit: int,
    now: str,
) -> RosterSnapshot:
    """Collect a roster snapshot, allowing unexpected transport errors to bubble."""
    checked_at = now
    environ = env if env is not None else os.environ
    request = http or default_http
    manifest_models = load_manifest_free_models(manifest_path or DEFAULT_MANIFEST)
    base_url = str(environ.get("OMNIROUTE_BASE_URL") or "").strip()
    api_key = _token("OMNIROUTE_API_KEY", env=environ)
    management_key = _token(
        "OMNIROUTE_MANAGEMENT_API_KEY",
        "OMNIROUTE_API_KEY",
        env=environ,
    )
    if not base_url or not (api_key or management_key):
        return stale_snapshot(
            detail=(
                "OmniRoute credentials unavailable; showing factory free-roster "
                "ids as unknown/stale. Free OpenCode models remain eligible."
            ),
            models=_unknown_models(
                manifest_models,
                detail="OmniRoute credentials unavailable",
            ),
            checked_at=checked_at,
            freshness="stale",
        )

    root = gateway_root(base_url)
    connections: list[ConnectionStatus] = []
    catalog_ids: list[str] = []
    catalog_known: bool = False
    notes: list[str] = []

    def fetch(path: str, token: str) -> HttpResult:
        url = urljoin(f"{root}/", path.lstrip("/"))
        return request("GET", url, token=token, timeout=timeout)

    if management_key:
        provider_result = fetch("/api/providers", management_key)
        if provider_result.status_code == 200:
            payload = _json_body(provider_result)
            if payload is not None:
                connections = extract_connections(payload)
            else:
                notes.append("provider list was not JSON")
        else:
            notes.append(
                f"provider list unavailable ({provider_result.status_code or provider_result.error})"
            )
        for path in ("/api/v1/models", "/api/models/catalog"):
            result = fetch(path, management_key)
            if result.status_code != 200:
                continue
            payload = _json_body(result)
            if payload is None:
                continue
            catalog_known = True
            for model_id in extract_model_ids(payload):
                if is_free_roster_model(model_id) and model_id not in catalog_ids:
                    catalog_ids.append(model_id)

    if not connections:
        connections = [
            ConnectionStatus(
                provider=name,
                active=None,
                test_status="unknown",
                detail="opencode and opencode-zen connections can both be active",
            )
            for name in CONNECTION_PROVIDERS
        ]

    probe_ids = build_probe_ids(manifest_models, catalog_ids, limit=probe_limit)
    catalog_set = {item.lower() for item in catalog_ids}
    models: list[RosterModel] = []
    chat_token = api_key or management_key

    for model_id in probe_ids:
        connection, _bare = split_model_id(model_id)
        in_catalog = None if not catalog_known else model_id.lower() in catalog_set
        if not chat_token:
            status = STATUS_CATALOG_MISS if in_catalog is False else STATUS_UNKNOWN
            detail = (
                "not in the active live catalog"
                if status == STATUS_CATALOG_MISS
                else "chat probe skipped; credentials missing"
            )
            models.append(
                RosterModel(
                    model_id=model_id,
                    connection=connection or "opencode",
                    status=status,
                    detail=detail,
                    in_catalog=in_catalog,
                )
            )
            continue
        try:
            result = request(
                "POST",
                urljoin(f"{root}/", "api/v1/chat/completions"),
                token=chat_token,
                payload={
                    "model": model_id,
                    "messages": [
                        {"role": "user", "content": "Reply with exactly: OPENCODE_FREE_ROSTER_OK"}
                    ],
                    "max_tokens": 8,
                    "stream": False,
                },
                timeout=timeout,
            )
        except Exception as exc:
            models.append(
                RosterModel(
                    model_id=model_id,
                    connection=connection or "opencode",
                    status=STATUS_ERROR,
                    detail=_brief_detail(None, str(exc), fallback="probe failed"),
                    in_catalog=in_catalog,
                )
            )
            continue
        cost = result.headers.get("x-omniroute-response-cost")
        if cost is None and result.body:
            parsed = _json_body(result)
            if isinstance(parsed, Mapping):
                usage = parsed.get("usage")
                if isinstance(usage, Mapping):
                    cost = usage.get("cost")
        status, detail = classify_probe_response(
            status_code=result.status_code,
            body=result.body,
            cost=cost,
            timed_out=result.timed_out,
            error=result.error,
        )
        models.append(
            RosterModel(
                model_id=model_id,
                connection=connection or "opencode",
                status=status,
                detail=detail,
                http_status=result.status_code,
                cost=None if cost is None else str(cost),
                in_catalog=in_catalog,
            )
        )

    freshness = "live" if any(row.status != STATUS_UNKNOWN for row in models) else "stale"
    if not models:
        freshness = "stale"
        notes.append("no free-roster models were discovered")
    detail = (
        "Live OmniRoute probe of free OpenCode / Zen models. "
        "opencode and opencode-zen connections can both be active. "
        "Free roster models stay eligible via catalog, pricing, and freeProviders; "
        "the opencode-zen providerOverride (blanket whole-provider=free) is avoided "
        "to prevent paid bleed."
    )
    if notes:
        detail = f"{detail} {' '.join(notes)}"
    return RosterSnapshot(
        checked_at=checked_at,
        freshness=freshness,
        detail=detail,
        connections=tuple(connections),
        models=tuple(models),
        counts=count_statuses(models),
    )


def main() -> int:
    """Print one JSON roster snapshot."""
    snapshot = collect_roster()
    print(json.dumps(snapshot.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
