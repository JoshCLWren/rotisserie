"""Operator-facing terminal output: readable, honest, and quiet by default.

Colors and the live "still waiting" line appear only on an interactive
terminal. ``NO_COLOR``, ``TERM=dumb``, CI and redirected output all produce
plain, line-oriented text. ``--json`` suppresses everything here; the command
prints one JSON document at the end instead.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TextIO


def duration(seconds: float) -> str:
    """Format elapsed time like 45s, 3m 05s or 2h 14m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    return f"{hours // 24}d {hours % 24}h"


def since(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "unknown"
    return duration((now - moment).total_seconds())


def local_time(moment: datetime | None) -> str:
    """Local wall-clock time, e.g. 'Sat 6:42 PM'."""
    if moment is None:
        return "unknown"
    value = moment.astimezone()
    stamp = value.strftime("%a %I:%M %p").replace(" 0", " ")
    if datetime.now().astimezone() - value > timedelta(days=6):
        stamp = value.strftime("%b %d ") + stamp
    return stamp


class Console:
    """Writes human output; every method is a no-op in JSON mode."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        json_mode: bool = False,
        verbose: bool = False,
        env: dict[str, str] | None = None,
        progress: TextIO | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.progress = progress
        self.json_mode = json_mode
        self.verbose = verbose
        values = dict(os.environ) if env is None else env
        tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.interactive = tty and values.get("TERM") != "dumb" and not values.get("CI")
        self.color = (self.interactive and "NO_COLOR" not in values) or bool(
            values.get("FORCE_COLOR")
        )
        encoding = (getattr(self.stream, "encoding", "") or "").lower()
        fancy = "utf" in encoding
        self.ok_mark = "✓" if fancy else "+"
        self.fail_mark = "✗" if fancy else "x"
        self.warn_mark = "!" if fancy else "!"
        self.dot = "•" if fancy else "-"
        self._live = False
        self._last_live = 0.0

    # Styling ---------------------------------------------------------------

    def _style(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def bold(self, text: str) -> str:
        return self._style(text, "1")

    def dim(self, text: str) -> str:
        return self._style(text, "2")

    def green(self, text: str) -> str:
        return self._style(text, "32")

    def red(self, text: str) -> str:
        return self._style(text, "31")

    def yellow(self, text: str) -> str:
        return self._style(text, "33")

    def cyan(self, text: str) -> str:
        return self._style(text, "36")

    # Output ----------------------------------------------------------------

    def _clear_live(self) -> None:
        if self._live:
            self.stream.write("\r\x1b[2K")
            self._live = False

    def line(self, text: str = "") -> None:
        if self.json_mode:
            return
        self._clear_live()
        self.stream.write(text + "\n")
        self.stream.flush()

    def heading(self, text: str) -> None:
        self.line()
        self.line(self.bold(text))
        self.line()

    def fields(self, rows: Sequence[tuple[str, str]]) -> None:
        width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            self.line(f"{self.dim(label.ljust(width))}   {value}")

    def ok(self, text: str, detail: str = "") -> None:
        self.line(
            f"{self.green(self.ok_mark)} {text}" + (f"  {self.dim(detail)}" if detail else "")
        )

    def fail(self, text: str, detail: str = "") -> None:
        self.line(
            f"{self.red(self.fail_mark)} {text}" + (f"  {self.dim(detail)}" if detail else "")
        )

    def warn(self, text: str, detail: str = "") -> None:
        self.line(
            f"{self.yellow(self.warn_mark)} {text}" + (f"  {self.dim(detail)}" if detail else "")
        )

    def note(self, text: str) -> None:
        self.line(f"{self.dim(self.dot)} {text}")

    def link(self, label: str, url: str) -> None:
        if url:
            self.line(f"{self.dim(label + ':')} {self.cyan(url)}")

    def debug(self, text: str) -> None:
        if self.verbose:
            self.line(self.dim(f"  [debug] {text}"))

    def waiting(self, text: str, *, interval: float = 60.0) -> None:
        """Show that nothing changed yet without flooding non-interactive logs."""
        if self.json_mode:
            return
        if self.interactive:
            self.stream.write(f"\r\x1b[2K{self.dim('… ' + text)}")
            self.stream.flush()
            self._live = True
            return
        now = time.monotonic()
        if now - self._last_live >= interval:
            self._last_live = now
            self.line(f"... {text}")

    def step(self, text: str, **fields: object) -> None:
        """One step of a long command: a JSON line on the progress stream, else a live line."""
        if self.progress is not None:
            self.progress.write(json.dumps({"step": text, **fields}, default=str) + "\n")
            self.progress.flush()
        elif self.interactive and not self.json_mode:
            self.waiting(text)

    def finish(self) -> None:
        self._clear_live()

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        if not rows:
            return
        widths = [len(header) for header in headers]
        for row in rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], len(cell))
        self.line(self.dim("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))))
        for row in rows:
            self.line("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


class Prompt:
    """Numbered menus over stdin; only used on an interactive terminal."""

    def __init__(self, console: Console, stream: TextIO | None = None) -> None:
        self.console = console
        self.input = stream or sys.stdin

    def _read(self, question: str) -> str:
        self.console.stream.write(question)
        self.console.stream.flush()
        reply = self.input.readline()
        if not reply:
            raise EOFError
        return reply.strip()

    def choose(self, heading: str, options: Sequence[str], *, default: int | None = None) -> int:
        self.console.line()
        self.console.line(self.console.bold(heading))
        for index, option in enumerate(options, start=1):
            self.console.line(f"  {index:>2}) {option}")
        hint = f" [{default + 1}]" if default is not None else ""
        while True:
            reply = self._read(f"Choose 1-{len(options)}{hint}: ")
            if not reply and default is not None:
                return default
            if reply.isdecimal() and 1 <= int(reply) <= len(options):
                return int(reply) - 1
            self.console.line("Type one of the numbers above.")

    def ask(self, question: str) -> str:
        return self._read(question + " ")

    def confirm(self, question: str, *, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        reply = self._read(f"{question} {suffix} ").lower()
        if not reply:
            return default
        return reply.startswith("y")
