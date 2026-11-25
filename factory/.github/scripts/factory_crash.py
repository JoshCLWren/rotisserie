#!/usr/bin/env python3
"""Factory crash capture: every entry point's uncaught exception becomes a visible, stored crash.

``run(main)`` installs a ``sys.excepthook`` and calls ``main``. On an uncaught
exception the hook prints the full traceback (the default hook), a GitHub
``::error`` annotation naming the exception, file, line and function, and
stores one ``factory_crash`` row: a stable fingerprint, a short scrubbed message
and the scrubbed traceback. The process then exits 1 exactly as before. A store
failure prints a warning and never replaces the original exception.

``item_failure`` isolates one item of a many-item pass: a GitHub call failure
(``GitHubCallError``, a gh timeout or truncated gh JSON) is printed, stored as a
non-fatal ``item`` row, and the pass continues. Every other exception, such as
NameError, UnboundLocalError, TypeError, AttributeError or KeyError, still
propagates and fails the run.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
MESSAGE_LIMIT = 300
TRACEBACK_LIMIT = 16384
STORE_TIMEOUT_SECONDS = 5.0
KINDS = ("python", "shell", "item")
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|_KEY$|TURSO_URL|DATABASE_URL")
SECRETS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:sk|nvapi|xox[abp])-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\b(?:libsql|wss)://\S+"),
    re.compile(r"(?i)https?://[^\s'\"]*turso\.io\S*"),
    re.compile(r"(?i)(?<=://)[^/\s:@'\"]+:[^/\s@'\"]+(?=@)"),
    re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(?<=token[=:] )\S{8,}|(?<=token=)\S{8,}"),
)


class GitHubCallError(RuntimeError):
    """A gh command failed or timed out: a GitHub-side failure, not a code error."""


TRANSIENT: tuple[type[BaseException], ...] = (
    GitHubCallError,
    subprocess.TimeoutExpired,
    json.JSONDecodeError,
)


@dataclass(frozen=True)
class Crash:
    fingerprint: str
    kind: str
    fatal: bool
    script: str
    function: str
    line: int
    exception_type: str
    message: str
    traceback: str
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("path")
        return data


def _secret_values(env: Mapping[str, str]) -> list[str]:
    values = [value for name, value in env.items() if SECRET_ENV.search(name) and len(value) >= 8]
    token_file = env.get("FACTORY_TURSO_TOKEN_FILE", "")
    if token_file:
        with contextlib.suppress(OSError):
            token = Path(token_file).read_text(encoding="utf-8").strip()
            if len(token) >= 8:
                values.append(token)
    return sorted(set(values), key=len, reverse=True)


def scrub(text: str, env: Mapping[str, str] | None = None) -> str:
    """Remove tokens, store URLs and credential values; keeps everything else readable."""
    for value in _secret_values(os.environ if env is None else env):
        text = text.replace(value, "<redacted>")
    for pattern in SECRETS:
        text = pattern.sub("<redacted>", text)
    return text


def short_message(text: str, env: Mapping[str, str] | None = None) -> str:
    flat = " ".join(scrub(text, env).split())
    return flat if len(flat) <= MESSAGE_LIMIT else flat[: MESSAGE_LIMIT - 1] + "…"


def cap_traceback(text: str, limit: int = TRACEBACK_LIMIT) -> str:
    """Keep the innermost frames: the end of a traceback names the failing line."""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    tail = tail[tail.find("\n") + 1 :] if "\n" in tail else tail
    return f"... {len(text) - len(tail)} characters of outer frames trimmed ...\n{tail}"


def fingerprint(kind: str, exception_type: str, script: str, function: str, line: int) -> str:
    key = f"{kind}|{exception_type}|{Path(script).name}|{function}|{line}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _factory_frame(frames: traceback.StackSummary) -> traceback.FrameSummary | None:
    """The innermost frame in a factory script; this module's own frames never count."""
    own = Path(__file__).resolve()
    inside = []
    for frame in frames:
        path = Path(frame.filename).resolve()
        if path != own and path.parent == SCRIPTS:
            inside.append(frame)
    if inside:
        return inside[-1]
    outside = [frame for frame in frames if Path(frame.filename).resolve() != own]
    return outside[-1] if outside else None


def describe(error: BaseException, *, kind: str = "python", fatal: bool = True) -> Crash:
    frames = traceback.extract_tb(error.__traceback__)
    frame = _factory_frame(frames)
    script = Path(frame.filename).name if frame else Path(sys.argv[0] or "<unknown>").name
    function = frame.name if frame else "<module>"
    line = int(frame.lineno or 0) if frame else 0
    name = type(error).__name__
    text = "".join(traceback.format_exception(error))
    return Crash(
        fingerprint=fingerprint(kind, name, script, function, line),
        kind=kind,
        fatal=fatal,
        script=script,
        function=function,
        line=line,
        exception_type=name,
        message=short_message(str(error)),
        traceback=cap_traceback(scrub(text)),
        path=frame.filename if frame else "",
    )


def _escape(value: str, *, prop: bool = False) -> str:
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return value.replace(":", "%3A").replace(",", "%2C") if prop else value


def headline(crash: Crash) -> str:
    where = f"{crash.script}:{crash.line} {crash.function}"
    return f"{crash.exception_type} in {where}: {crash.message}".rstrip(": ")


def annotate(crash: Crash, *, level: str = "error", stream: Any = None) -> None:
    """A GitHub annotation on stderr: stdout is often data (``$(...)`` and pipes)."""
    out = sys.stderr if stream is None else stream
    path = crash.path or crash.script
    with contextlib.suppress(ValueError):
        path = os.path.relpath(path)
    title = "Factory crash" if crash.fatal else "Factory item failed"
    props = f"file={_escape(path, prop=True)},line={crash.line},title={_escape(title, prop=True)}"
    print(f"::{level} {props}::{_escape(headline(crash))}", file=out, flush=True)


def workflow_file(env: Mapping[str, str]) -> str:
    ref = env.get("GITHUB_WORKFLOW_REF", "")
    if ref:
        return ref.split("@", 1)[0].rsplit("/", 1)[-1]
    return env.get("GITHUB_WORKFLOW", "")


def run_url(env: Mapping[str, str]) -> str:
    run_id, repo = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_REPOSITORY", "")
    if not run_id or not repo:
        return ""
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    suffix = f"/attempts/{attempt}" if attempt and attempt != "1" else ""
    return f"{server}/{repo}/actions/runs/{run_id}{suffix}"


INSERT = (
    "INSERT INTO factory_crash (repo, fingerprint, kind, fatal, script, function, line,"
    " exception_type, message, traceback, workflow, job, run_id, run_url, created_at)"
    " VALUES (:repo, :fingerprint, :kind, :fatal, :script, :function, :line, :exception_type,"
    " :message, :traceback, :workflow, :job, :run_id, :run_url, :created_at)"
)


def row(crash: Crash, env: Mapping[str, str], now: int) -> dict[str, Any]:
    return {
        **crash.to_dict(),
        "fatal": int(crash.fatal),
        "repo": env.get("GITHUB_REPOSITORY", ""),
        "workflow": workflow_file(env),
        "job": env.get("GITHUB_JOB", ""),
        "run_id": env.get("GITHUB_RUN_ID", ""),
        "run_url": run_url(env),
        "created_at": now,
    }


def _schema(libsql: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(sql, {}) for sql in libsql.schema_statements() if "factory_crash" in sql]


def record(crash: Crash, env: Mapping[str, str] | None = None, store: Any = None) -> bool:
    """Store one occurrence; False when no store is configured or the write failed."""
    env = os.environ if env is None else env
    try:
        import factory_libsql as libsql
    except ImportError:
        return False
    try:
        if store is None:
            if not libsql.configured():
                return False
            store = libsql.connect()
            if hasattr(store, "timeout"):
                store.timeout = STORE_TIMEOUT_SECONDS
        if not env.get("GITHUB_REPOSITORY"):
            return False
        store.batch([*_schema(libsql), (INSERT, row(crash, env, int(time.time())))])
        return True
    except Exception as exc:  # noqa: BLE001 - the original failure must stay the reported one
        print(
            f"::warning title=Factory crash not stored::{type(exc).__name__}: "
            f"{_escape(short_message(str(exc)))}",
            file=sys.stderr,
            flush=True,
        )
        return False


def report(error: BaseException, *, kind: str = "python", fatal: bool = True) -> Crash | None:
    try:
        crash = describe(error, kind=kind, fatal=fatal)
        annotate(crash, level="error" if fatal else "warning")
        record(crash)
        return crash
    except Exception as exc:  # noqa: BLE001 - never replace the exception being reported
        print(f"::warning::factory crash capture failed: {type(exc).__name__}", file=sys.stderr)
        return None


def _hook(
    previous: Callable[[type[BaseException], BaseException, TracebackType | None], Any],
) -> Callable[[type[BaseException], BaseException, TracebackType | None], None]:
    def hook(kind: type[BaseException], error: BaseException, tb: TracebackType | None) -> None:
        previous(kind, error, tb)
        if not issubclass(kind, KeyboardInterrupt):
            if error.__traceback__ is None:
                error = error.with_traceback(tb)
            report(error)

    hook.factory_crash = True  # type: ignore[attr-defined]
    return hook


def install() -> None:
    if not getattr(sys.excepthook, "factory_crash", False):
        sys.excepthook = _hook(sys.excepthook)


def run(main: Callable[..., Any], *args: Any) -> Any:
    """Install the hook, then run ``main``: ``raise SystemExit(factory_crash.run(main))``."""
    install()
    return main(*args)


@contextlib.contextmanager
def item_failure(
    label: str, transient: tuple[type[BaseException], ...] = TRANSIENT
) -> Iterator[None]:
    """Isolate one item of a pass: a GitHub failure is reported and skipped, code errors raise."""
    try:
        yield
    except transient as error:
        print(f"[factory] {label} failed; continuing with the next item", file=sys.stderr)
        traceback.print_exception(error, file=sys.stderr)
        report(error, kind="item", fatal=False)


def item(label: str, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """``call(*args, **kwargs)``, or None when ``item_failure`` isolated a GitHub failure."""
    with item_failure(label):
        return call(*args, **kwargs)
    return None


def shell_crash(
    script: str, line: int, function: str, command: str, status: int, trace: str = ""
) -> Crash:
    name = f"exit {status}"
    text = f"{Path(script).name}:{line} {function}: `{command}` exited {status}"
    body = scrub(trace or text)
    return Crash(
        fingerprint=fingerprint("shell", name, script, function, line),
        kind="shell",
        fatal=True,
        script=Path(script).name,
        function=function,
        line=line,
        exception_type=name,
        message=short_message(f"`{command}` exited {status}"),
        traceback=cap_traceback(body),
        path=script,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    shell = commands.add_parser("record-shell", help="store one failed shell command")
    shell.add_argument("--script", required=True)
    shell.add_argument("--line", type=int, default=0)
    shell.add_argument("--function", default="main")
    shell.add_argument("--command", dest="failed", default="")
    shell.add_argument("--status", type=int, default=1)
    shell.add_argument("--trace", default="")
    export = commands.add_parser("export", help="recent crash groups as JSON, no tracebacks")
    export.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    export.add_argument("--hours", type=int, default=24)
    args = parser.parse_args(argv)
    if args.command == "record-shell":
        crash = shell_crash(
            args.script, args.line, args.function, args.failed, args.status, args.trace
        )
        record(crash)
        return 0
    import factory_crash_store as crash_store
    import factory_libsql as libsql

    found = crash_store.export(libsql.connect(), args.repo, hours=args.hours)
    print(json.dumps(found, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
