#!/usr/bin/env python3
"""Install Rotisserie factory workflows into an adopter repository.

Each template under factory/.github/workflows is written to the adopter's
.github/workflows with the same file name. After every step that checks out the
adopter repository, two steps are inserted: a checkout of Rotisserie at --ref
(main by default, or a pinned commit), then overlay.sh, which copies the factory
runtime into the workspace.
Templates are edited as text so comments and formatting survive.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = FACTORY_ROOT / ".github" / "workflows"
DEFAULT_SOURCE = "JoshCLWren/rotisserie"
RUNTIME_DIR = ".rotisserie/runtime"
INSTALLED_MARKER = "rotisserie-factory-runtime"
# These templates only run the factory's own test suite, which Rotisserie CI
# already runs; in an adopter they would fail because tests are not overlaid.
ROTISSERIE_CI_ONLY = frozenset({"factory-fibonacci-tests.yml", "factory-model-contracts.yml"})
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}\Z")
DEFAULT_REF = "main"
STEP_RE = re.compile(r"^(?P<indent>\s*)- (?P<rest>.*)$")
CHECKOUT_RE = re.compile(r"^\s*(?:- )?uses:\s*actions/checkout@\S+\s*$")


class InstallError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    start: int
    end: int
    indent: str


def _steps(lines: list[str]) -> list[Step]:
    steps: list[Step] = []
    for index, line in enumerate(lines):
        match = STEP_RE.match(line)
        if not match:
            continue
        indent = match["indent"]
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            stripped = candidate.strip()
            if stripped and not stripped.startswith("#"):
                leading = len(candidate) - len(candidate.lstrip())
                if leading <= len(indent):
                    break
            end += 1
        while end > index + 1 and not lines[end - 1].strip():
            end -= 1
        steps.append(Step(index, end, indent))
    return steps


def _value(block: list[str], key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}:\s*(.*?)\s*$")
    for line in block:
        match = pattern.match(line)
        if match:
            return match.group(1).strip("'\"")
    return None


def _checks_out_adopter(block: list[str]) -> bool:
    if not any(CHECKOUT_RE.match(line) for line in block):
        return False
    repository = _value(block, "repository")
    return repository in (None, "${{ github.repository }}")


def _condition(block: list[str], indent: str) -> list[str]:
    """Return the step-level `if:` lines (with continuations) of a checkout step."""
    key = indent + "  if:"
    first = indent + "- if:"
    for index, line in enumerate(block):
        if line.startswith(key) or line.startswith(first):
            text = indent + "  " + line.strip().removeprefix("- ") + "\n"
            lines = [text]
            for follow in block[index + 1 :]:
                if follow.strip() and len(follow) - len(follow.lstrip()) > len(indent) + 2:
                    lines.append(follow if follow.endswith("\n") else follow + "\n")
                else:
                    break
            return lines
    return []


def _overlay_steps(
    indent: str, source: str, ref: str, checkout_path: str | None, condition: list[str]
) -> list[str]:
    target = checkout_path or "."
    runtime = f"{checkout_path}/{RUNTIME_DIR}" if checkout_path else RUNTIME_DIR
    body = indent + "  "
    return [
        f"{indent}- name: Check out Rotisserie factory runtime  # {INSTALLED_MARKER}\n",
        *condition,
        f"{body}uses: actions/checkout@v7\n",
        f"{body}with:\n",
        f"{body}  repository: {source}\n",
        f"{body}  ref: {ref}\n",
        f"{body}  path: {runtime}\n",
        f"{body}  persist-credentials: false\n",
        f"{indent}- name: Overlay Rotisserie factory runtime  # {INSTALLED_MARKER}\n",
        *condition,
        f"{body}run: bash {runtime}/factory/install/overlay.sh {target}\n",
    ]


def render(template: str, *, source: str, ref: str) -> str:
    if not (SHA_RE.fullmatch(ref) or (BRANCH_RE.fullmatch(ref) and ".." not in ref)):
        raise InstallError("ref must be a branch name or a full 40-character commit SHA")
    lines = template.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    insertions: list[tuple[int, list[str]]] = []
    for step in _steps(lines):
        block = lines[step.start : step.end]
        if not _checks_out_adopter(block):
            continue
        path = _value(block, "path")
        checkout_path = None if path in (None, ".", "") else path
        condition = _condition(block, step.indent)
        insertions.append(
            (step.end, _overlay_steps(step.indent, source, ref, checkout_path, condition))
        )
    for position, new_lines in reversed(insertions):
        lines[position:position] = new_lines
    return "".join(lines)


def install(target: Path, *, source: str, ref: str, names: list[str] | None = None) -> list[Path]:
    workflows = target / ".github" / "workflows"
    if not (target / ".git").exists():
        raise InstallError(f"{target} is not a git checkout")
    templates = sorted(TEMPLATES.glob("*.yml"))
    if not names:
        templates = [path for path in templates if path.name not in ROTISSERIE_CI_ONLY]
    if names:
        wanted = set(names)
        unknown = wanted - {path.name for path in templates}
        if unknown:
            raise InstallError(f"unknown workflow templates: {', '.join(sorted(unknown))}")
        templates = [path for path in templates if path.name in wanted]
    workflows.mkdir(parents=True, exist_ok=True)
    written = []
    for template in templates:
        destination = workflows / template.name
        destination.write_text(
            render(template.read_text(encoding="utf-8"), source=source, ref=ref), encoding="utf-8"
        )
        written.append(destination)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="adopter repository checkout")
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help="Rotisserie branch to track (default: main) or a commit SHA to pin",
    )
    parser.add_argument(
        "--source", default=DEFAULT_SOURCE, help="Rotisserie repository (owner/name)"
    )
    parser.add_argument(
        "--workflow", action="append", dest="workflows", help="install only this template"
    )
    args = parser.parse_args(argv)
    try:
        written = install(
            args.target.resolve(), source=args.source, ref=args.ref, names=args.workflows
        )
    except InstallError as exc:
        print(f"install: {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
