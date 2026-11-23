"""Small typed wrapper over the authenticated GitHub CLI (``gh``).

Using ``gh`` keeps the operator's existing login, SSO and token handling. Every
failure is translated into one of three plain errors so commands can explain
what went wrong instead of printing raw ``gh`` output.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

type JSON = dict[str, JSON] | list[JSON] | str | int | float | bool | None
type Obj = dict[str, JSON]

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class GitHubError(RuntimeError):
    """GitHub could not complete a request."""

    exit_code = 5


class GitHubAuthError(GitHubError):
    """The GitHub CLI is missing or not logged in with enough access."""


class GitHubRateLimitError(GitHubError):
    """GitHub throttled the request. A retry time may or may not be known."""

    def __init__(
        self,
        message: str,
        *,
        secondary: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.secondary = secondary
        self.retry_after_seconds = retry_after_seconds


class GitHubNotFoundError(GitHubError):
    """The requested GitHub object does not exist or is not visible."""


def _classify(message: str) -> GitHubError:
    lower = message.lower()
    if "rate limit" in lower or "abuse detection" in lower:
        secondary = "secondary rate limit" in lower or "abuse detection" in lower
        # `gh` may include the server's Retry-After response in the error text.
        match = re.search(r"retry[- ]after\\s*[:=]?\\s*(\\d+)\\s*(?:seconds?|s)?", lower)
        retry = int(match.group(1)) if match else None
        if retry is None:
            match = re.search(r"retry (?:the request )?after (\\d+) seconds?", lower)
            retry = int(match.group(1)) if match else None
        return GitHubRateLimitError(
            "GitHub is applying a secondary rate limit."
            if secondary else "GitHub's API rate limit is exhausted.",
            secondary=secondary,
            retry_after_seconds=retry,
        )
    if (
        "gh auth login" in lower
        or "authentication" in lower
        or "http 401" in lower
        or "bad credentials" in lower
        or "must authenticate" in lower
    ):
        return GitHubAuthError("GitHub CLI is not logged in. Run: gh auth login")
    if "http 403" in lower and ("permission" in lower or "resource not accessible" in lower):
        return GitHubAuthError(
            "Your GitHub login lacks permission for this repository "
            "(it needs repo and workflow access). Run: gh auth refresh -s repo,workflow"
        )
    if "http 404" in lower or "not found" in lower or "could not resolve to" in lower:
        return GitHubNotFoundError("GitHub says that does not exist or you cannot see it.")
    detail = message.strip().splitlines()[-1] if message.strip() else "no details"
    return GitHubError(f"GitHub request failed: {detail}")


class GitHub:
    """Repository-scoped GitHub access through ``gh``."""

    def __init__(self, repo: str, *, executable: str = "gh", timeout: float = 60) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError(f"Repository must look like owner/name, not {repo!r}")
        self.repo = repo
        self.executable = executable
        self.timeout = timeout
        self.calls = 0

    def _run(
        self, args: Sequence[str], *, stdin: str | None = None, allow_exit_one: bool = False
    ) -> str:
        if shutil.which(self.executable) is None:
            raise GitHubAuthError(
                "The GitHub CLI (gh) is not installed. See https://cli.github.com"
            )
        self.calls += 1
        try:
            completed = subprocess.run(
                [self.executable, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitHubError("GitHub did not answer in time. Check your connection.") from None
        except OSError as exc:
            raise GitHubError(f"Could not run the GitHub CLI: {exc}") from None
        if completed.returncode != 0 and not (allow_exit_one and completed.returncode == 1):
            raise _classify(completed.stderr or completed.stdout)
        return completed.stdout

    def api(self, path: str, *, method: str = "GET", fields: dict[str, str] | None = None) -> JSON:
        args = ["api", path]
        if method != "GET":
            args[1:1] = ["--method", method]
        for key, value in (fields or {}).items():
            args += ["-f", f"{key}={value}"]
        output = self._run(args)
        if not output.strip():
            return None
        try:
            return cast(JSON, json.loads(output))
        except ValueError:
            raise GitHubError("GitHub returned something that is not JSON.") from None

    def repo_api(self, path: str, **kwargs: object) -> JSON:
        method = kwargs.get("method", "GET")
        fields = kwargs.get("fields")
        return self.api(
            f"repos/{self.repo}/{path}",
            method=str(method),
            fields=cast(dict[str, str] | None, fields),
        )

    def text(self, path: str) -> str:
        """Fetch a text endpoint such as job logs, with terminal escapes removed."""
        output = self._run(["api", "--allow-escape-sequences", f"repos/{self.repo}/{path}"])
        return ANSI.sub("", output)

    def gh(self, *args: str, stdin: str | None = None, allow_exit_one: bool = False) -> str:
        if allow_exit_one:
            return self._run(list(args), stdin=stdin, allow_exit_one=True)
        return self._run(list(args), stdin=stdin)

    def workflow_run(self, workflow: str, inputs: dict[str, str], *, ref: str = "main") -> str:
        args = ["workflow", "run", workflow, "--repo", self.repo, "--ref", ref]
        for key, value in inputs.items():
            args += ["-f", f"{key}={value}"]
        return self._run(args)

    def whoami(self) -> str:
        user = self.api("user")
        login = obj(user).get("login")
        if not isinstance(login, str):
            raise GitHubAuthError("GitHub CLI is not logged in. Run: gh auth login")
        return login


# Typed accessors for untyped GitHub JSON -----------------------------------


def obj(value: JSON) -> Obj:
    return value if isinstance(value, dict) else {}


def items(value: JSON, key: str | None = None) -> list[Obj]:
    if key is not None:
        value = obj(value).get(key)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def string(value: Obj, key: str) -> str:
    found = value.get(key)
    return found if isinstance(found, str) else ""


def integer(value: Obj, key: str) -> int | None:
    found = value.get(key)
    return found if isinstance(found, int) and not isinstance(found, bool) else None


def timestamp(value: Obj, key: str) -> datetime | None:
    raw = string(value, key)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def label_names(value: Obj) -> set[str]:
    names: set[str] = set()
    for label in items(value, "labels"):
        name = string(label, "name")
        if name:
            names.add(name)
    return names
