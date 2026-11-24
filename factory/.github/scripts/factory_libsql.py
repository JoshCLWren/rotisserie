"""Factory coordination store on libSQL (Turso) or a plain SQLite file, standard library only.

One request is one atomic unit. ``batch`` sends BEGIN IMMEDIATE, the
statements and COMMIT in one Hrana pipeline request (or one local SQLite
transaction), so no transaction ever stays open across a network call, a
GitHub call or a model session. Callers make every authorizing check part of
the write statements themselves (compare-and-swap), never a prior read.

Errors fail closed. Only recognized write contention is retried, with bounded
jittered backoff. A response lost after the request was sent raises
``StoreAmbiguous``: the caller reconciles by its claim or event identity.
Quota exhaustion (``BLOCKED``) raises ``StoreBlocked`` and is never retried.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA = Path(__file__).resolve().parents[1] / "factory-state.sqlite.sql"
SCHEMA_VERSION = 1
URL_ENV = "FACTORY_TURSO_URL"
TOKEN_FILE_ENV = "FACTORY_TURSO_TOKEN_FILE"
FILE_ENV = "FACTORY_STATE_SQLITE"
TIMEOUT_SECONDS = 10.0
RETRIES = 6
Params = Mapping[str, Any]
Statement = tuple[str, Params]
Row = dict[str, Any]


class StoreError(RuntimeError):
    """The store refused or failed a request; nothing may be assumed written."""


class StoreUnavailable(StoreError):
    """The store could not be reached; the request was not delivered."""


class StoreBlocked(StoreError):
    """The free quota is exhausted; every write and read is refused until reset."""


class StoreContention(StoreError):
    """Another writer held the write lock; the request was rolled back."""


class StoreAmbiguous(StoreError):
    """The request was sent but its result was lost; it may or may not have committed."""


def _classify(message: str) -> type[StoreError]:
    text = message.upper()
    if "BLOCKED" in text:
        return StoreBlocked
    if "SQLITE_BUSY" in text or "DATABASE IS LOCKED" in text or "SQLITE_LOCKED" in text:
        return StoreContention
    return StoreError


def _encode(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _decode(value: Mapping[str, Any]) -> Any:
    kind = value.get("type")
    if kind == "integer":
        return int(value["value"])
    if kind == "float":
        return float(value["value"])
    if kind == "text":
        return value["value"]
    if kind == "null":
        return None
    raise StoreError(f"unsupported libSQL value type {kind}")


def used(sql: str, params: Params) -> dict[str, Any]:
    """Only the parameters the statement names; libSQL rejects unused ones."""
    names = set(re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", sql))
    return {key: value for key, value in params.items() if key in names}


def _stmt(sql: str, params: Params) -> dict[str, Any]:
    named = [
        {"name": f":{key}", "value": _encode(value)} for key, value in used(sql, params).items()
    ]
    return {"sql": sql, "named_args": named, "want_rows": True}


def _rows(result: Mapping[str, Any]) -> list[Row]:
    names = [column["name"] for column in result.get("cols", [])]
    return [
        dict(zip(names, (_decode(v) for v in row), strict=True)) for row in result.get("rows", [])
    ]


class Store:
    """Common retry and accounting; transports implement ``_send_batch`` and ``_send_reads``."""

    def __init__(self) -> None:
        self.usage = {"requests": 0, "rows_read": 0, "rows_written": 0}

    def read(self, sql: str, params: Params | None = None) -> list[Row]:
        return self.reads([(sql, params or {})])[0]

    def reads(self, statements: Sequence[Statement]) -> list[list[Row]]:
        """Several reads in one request, each its own autocommit statement."""
        return self._retry(lambda: self._send_reads(statements))

    def batch(self, statements: Sequence[Statement]) -> list[list[Row]]:
        """All statements in one write transaction: every one commits or none does."""
        return self._retry(lambda: self._send_batch(statements))

    def _retry(self, call: Callable[[], list[list[Row]]]) -> list[list[Row]]:
        for attempt in range(RETRIES):
            try:
                return call()
            except StoreContention:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(random.uniform(0.02, 0.05 * 2**attempt))
        raise StoreContention("write contention retries exhausted")

    def _send_reads(self, statements: Sequence[Statement]) -> list[list[Row]]:
        raise NotImplementedError

    def _send_batch(self, statements: Sequence[Statement]) -> list[list[Row]]:
        raise NotImplementedError


class FileStore(Store):
    """A local SQLite file: tests, local factories and portable backups."""

    def __init__(self, path: str | Path, *, busy_ms: int = 5000) -> None:
        super().__init__()
        self.path = str(path)
        self.busy_ms = busy_ms

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_ms / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, statements: Sequence[Statement], *, write: bool) -> list[list[Row]]:
        self.usage["requests"] += 1
        try:
            conn = self._connect()
        except sqlite3.Error as error:
            raise StoreUnavailable(str(error)) from error
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            results = []
            for sql, params in statements:
                cursor = conn.execute(sql, used(sql, params))
                results.append([dict(row) for row in cursor.fetchall()])
                if write and cursor.rowcount > 0:
                    self.usage["rows_written"] += cursor.rowcount
            if write:
                conn.execute("COMMIT")
            return results
        except sqlite3.Error as error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _classify(str(error))(str(error)) from error
        finally:
            conn.close()

    def _send_reads(self, statements: Sequence[Statement]) -> list[list[Row]]:
        return self._run(statements, write=False)

    def _send_batch(self, statements: Sequence[Statement]) -> list[list[Row]]:
        return self._run(statements, write=True)


class HttpStore(Store):
    """A libSQL server (sqld or Turso Cloud) over the Hrana HTTP pipeline API."""

    def __init__(self, url: str, token: str = "", *, timeout: float = TIMEOUT_SECONDS) -> None:
        super().__init__()
        parts = urlsplit(url)
        scheme = "https" if parts.scheme in ("libsql", "https") else parts.scheme
        local = parts.hostname in ("127.0.0.1", "localhost", "::1")
        if scheme not in ("https", "http") or (scheme == "http" and not local):
            raise ValueError("libSQL URL must use https (http only for a local server)")
        self.endpoint = f"{scheme}://{parts.netloc}/v2/pipeline"
        self._token = token
        self.timeout = timeout
        self.trace: list[tuple[str, int, int]] | None = None

    def _post(self, requests: list[dict[str, Any]], *, write: bool) -> list[dict[str, Any]]:
        body = json.dumps({"baton": None, "requests": [*requests, {"type": "close"}]}).encode()
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        self.usage["requests"] += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise _classify(f"{error.code} {detail}")(
                f"libSQL HTTP {error.code}: {detail}"
            ) from None
        except urllib.error.URLError as error:
            reason = error.reason
            if isinstance(reason, ConnectionRefusedError | socket.gaierror):
                raise StoreUnavailable(f"libSQL unreachable ({type(reason).__name__})") from None
            failure = StoreAmbiguous if write else StoreUnavailable
            raise failure(f"libSQL request lost ({type(reason).__name__})") from None
        except (TimeoutError, http.client.HTTPException, ConnectionError, ValueError) as error:
            failure = StoreAmbiguous if write else StoreUnavailable
            raise failure(f"libSQL response lost ({type(error).__name__})") from None
        results = payload.get("results") or []
        for item in results:
            if item.get("type") == "error":
                message = str(item.get("error", {}).get("message", "libSQL error"))
                raise _classify(message)(message)
        return [item["response"] for item in results[:-1]]

    def _count(self, result: Mapping[str, Any], sql: str = "") -> None:
        read, written = int(result.get("rows_read") or 0), int(result.get("rows_written") or 0)
        self.usage["rows_read"] += read
        self.usage["rows_written"] += written
        if self.trace is not None:
            self.trace.append((" ".join(sql.split())[:120], read, written))

    def _send_reads(self, statements: Sequence[Statement]) -> list[list[Row]]:
        requests = [{"type": "execute", "stmt": _stmt(sql, params)} for sql, params in statements]
        out = []
        for (sql, _), response in zip(statements, self._post(requests, write=False), strict=True):
            self._count(response["result"], sql)
            out.append(_rows(response["result"]))
        return out

    def _send_batch(self, statements: Sequence[Statement]) -> list[list[Row]]:
        steps: list[dict[str, Any]] = [{"stmt": {"sql": "BEGIN IMMEDIATE"}}]
        for sql, params in statements:
            steps.append(
                {"stmt": _stmt(sql, params), "condition": {"type": "ok", "step": len(steps) - 1}}
            )
        commit = len(steps)
        steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": commit - 1}})
        steps.append(
            {
                "stmt": {"sql": "ROLLBACK"},
                "condition": {"type": "not", "cond": {"type": "ok", "step": commit}},
            }
        )
        (response,) = self._post([{"type": "batch", "batch": {"steps": steps}}], write=True)
        result = response["result"]
        errors = [e for e in result.get("step_errors", []) if e]
        if errors:
            message = str(errors[0].get("message", "libSQL batch step failed"))
            raise _classify(message)(message)
        outcomes = result["step_results"]
        if outcomes[commit] is None:
            raise StoreError("libSQL batch did not commit")
        for sql, step in zip(
            ["BEGIN", *(sql for sql, _ in statements), "COMMIT"], outcomes, strict=False
        ):
            if step:
                self._count(step, sql)
        return [_rows(step) for step in outcomes[1:commit]]


def configured() -> bool:
    return bool(os.environ.get(URL_ENV) or os.environ.get(FILE_ENV))


def connect() -> Store:
    """The store named by the trusted environment. The token is read from a file, never env."""
    url = os.environ.get(URL_ENV, "")
    if url:
        token_file = os.environ.get(TOKEN_FILE_ENV, "")
        token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else ""
        return HttpStore(url, token)
    path = os.environ.get(FILE_ENV, "")
    if path:
        return FileStore(path)
    raise StoreUnavailable("factory store is not configured")


def schema_statements(text: str | None = None) -> list[str]:
    source = SCHEMA.read_text(encoding="utf-8") if text is None else text
    lines = [line for line in source.splitlines() if not line.lstrip().startswith("--")]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def ensure_schema(store: Store) -> bool:
    """Create the schema once; returns True when this call created it."""
    try:
        found = store.read("SELECT max(version) AS v FROM _schema_version")
    except StoreBlocked:
        raise
    except StoreError:
        found = []
    if found and found[0]["v"] is not None and int(found[0]["v"]) >= SCHEMA_VERSION:
        return False
    store.batch([(sql, {}) for sql in schema_statements()])
    return True


def placeholders(prefix: str, values: Iterable[Any]) -> tuple[str, dict[str, Any]]:
    """``:p0, :p1`` and their parameters, for an IN list of known length."""
    params = {f"{prefix}{index}": value for index, value in enumerate(values)}
    return ", ".join(f":{name}" for name in params), params
