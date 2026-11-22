"""Shared state for one console command: repository, GitHub, output."""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.tracker import Clock
from rotisserie.factory_console.ui import Console, Prompt


class UsageError(ValueError):
    """The operator asked for something that cannot work as written."""


@dataclass
class Context:
    github: GitHub
    console: Console
    prompt: Prompt
    clock: Clock
    interactive: bool
    # What the command has learned so far; survives Ctrl+C for the resume hint.
    progress: dict[str, object] = field(default_factory=dict)

    @property
    def repo(self) -> str:
        return self.github.repo

    def url(self, path: str = "") -> str:
        return f"https://github.com/{self.repo}{path}"


def find_repository(explicit: str | None, env: dict[str, str], cwd: Path) -> str:
    """Pick the adopter repository: flag, FACTORY_REPO, .rotisserie/config.toml, git remote."""
    if explicit:
        return explicit
    if env.get("FACTORY_REPO"):
        return env["FACTORY_REPO"]
    for directory in (cwd, *cwd.parents):
        config = directory / ".rotisserie" / "config.toml"
        if config.is_file():
            try:
                data = tomllib.loads(config.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                break
            repository = data.get("repository")
            if isinstance(repository, dict):
                owner, name = repository.get("owner"), repository.get("name")
                if isinstance(owner, str) and isinstance(name, str):
                    return f"{owner}/{name}"
            break
    try:
        remote = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        remote = ""
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
    if match:
        return match.group(1)
    raise UsageError(
        "Could not tell which repository runs the factory. Pass --repo owner/name, "
        "set FACTORY_REPO, or run inside a repository with .rotisserie/config.toml."
    )
