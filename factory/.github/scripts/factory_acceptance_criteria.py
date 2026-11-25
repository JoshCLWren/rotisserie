"""Issue criteria extraction, deferral and citation rules for exact-head acceptance.

Context headings: a bug report's observed behavior, repro steps, related work
and verbatim reporter evidence describe the problem. No changed file can prove
them, so they are never criteria. A heading is context when one of its
segments (split on ``/``, ``:``, ``(`` and ``)``) starts with a context word,
unless a segment names a criteria section such as "requirements".

Verify after merge: a criterion that asks for verification in a deployed
environment (``in a deployed browser``, ``in production``, ``after deploy``,
``post-merge``) cannot be proven by a pre-merge diff. It stays numbered and
visible, never blocks approval, and post-merge closure keeps the issue open
until its checkbox is checked. Both a verification verb and a deployed
environment phrase are required, so a criterion that only mentions
production code stays strict.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

HEADINGS = (
    "scope",
    "implementation required",
    "requirements",
    "acceptance",
    "verification before",
    "required evidence",
    "completion evidence",
    "expected behavior",
    "outcome",
)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
CONTEXT_HEADINGS = (
    "observed",
    "repro",
    "steps to reproduce",
    "notes",
    "related",
    "background",
    "context",
    "original report",
    "original reporter",
    "reporter evidence",
    "verbatim",
)
_SEGMENT_SPLIT = re.compile(r"[/:()]")
_CONTEXT_START = re.compile(r"^(?:" + "|".join(map(re.escape, CONTEXT_HEADINGS)) + r")\b")
_CRITERIA_WORD = re.compile(r"\b(?:" + "|".join(map(re.escape, HEADINGS)) + r")\b")
_VERIFY = re.compile(
    r"(?i)\b(?:verify|verified|verification|confirm|confirmed|record|observe|smoke[- ]test)\b"
)
_DEPLOYED = re.compile(
    r"(?i)\b(?:in|on|against)\s+(?:an?\s+|the\s+)?(?:[\w-]+\s+){0,2}deployed\b"
    r"|\b(?:in|on|against)\s+(?:the\s+)?production\b(?!\s+code)"
    r"|\bafter\s+(?:the\s+)?(?:deploy|deployment|release|merge)\b"
    r"|\bpost-(?:deploy|deployment|merge)\b"
)
VERIFY_AFTER_MERGE = "verify_after_merge"
_PART_OF = re.compile(r"Part of #\d+(?:\s+and\s+governed by #\d+)?\.")
_CITATION_SPLIT = re.compile(r",(?!\d)\s*|;\s*|\s+and\s+")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")
_RANGE_ONLY = re.compile(r"^\d+(?:-\d+)?$")
_CHECKED = re.compile(r"(?m)^\s*[-*+]\s+\[[xX]\]\s+(.*\S)\s*$")
FAILURE_LIMIT = 20


@dataclass(frozen=True)
class Criterion:
    """One numbered issue requirement and how acceptance treats it."""

    text: str
    verify_after_merge: bool = False


def is_context_heading(heading: str) -> bool:
    """Return whether a heading introduces context rather than criteria."""
    segments = [part.strip() for part in _SEGMENT_SPLIT.split(heading.lower()) if part.strip()]
    if any(_CRITERIA_WORD.search(part) for part in segments):
        return False
    return any(_CONTEXT_START.match(part) for part in segments)


def is_verify_after_merge(text: str) -> bool:
    """Return whether a criterion can only be verified in a deployed environment."""
    return bool(_VERIFY.search(text) and _DEPLOYED.search(text))


def requirements(body: str) -> list[str]:
    """Preserve every issue requirement, including unrecognized headings and prose.

    Args:
        body: Complete authoritative issue body.

    Returns:
        Ordered paragraphs and bullets with their heading context.

    Raises:
        ValueError: If the issue provides no reviewable requirement text.
    """
    section = "Issue"
    entries: list[tuple[bool, str]] = []
    current: list[str] = []

    def flush() -> None:
        """Finish an entry without losing continuation lines."""
        if current:
            entries.append((is_context_heading(section), f"{section}: {' '.join(current)}"))
            current.clear()

    for source in body.splitlines():
        line = source.strip()
        if line.startswith("<!--") and line.endswith("-->"):
            continue
        heading = HEADING.match(source)
        if heading:
            flush()
            section = heading.group(1).strip()
            continue
        if not line:
            flush()
            continue
        item = BULLET.match(source)
        if item:
            flush()
            line = re.sub(r"^\[[ xX]\]\s*", "", item.group(1)).strip()
        # Dependency links are verified by the separate factory dependency gate.
        if re.fullmatch(r"#\d+", line) or re.fullmatch(
            r"Part of #\d+(?:\s+and\s+governed by #\d+)?\.", line
        ):
            continue
        if line:
            current.append(line)
    flush()
    if not entries:
        raise ValueError("Issue has no reviewable criteria, clarification required")
    criteria = [text for context, text in entries if not context]
    return criteria or [text for _, text in entries]


def criteria(body: str) -> list[Criterion]:
    """Number-aligned criteria with their verify-after-merge classification."""
    return [Criterion(text, is_verify_after_merge(text)) for text in requirements(body)]


def deferred(body: str) -> list[tuple[str, str]]:
    """Return ``(id, text)`` for every criterion verified only after merge."""
    return [
        (f"A{index}", item.text)
        for index, item in enumerate(criteria(body), 1)
        if item.verify_after_merge
    ]


def pending_after_merge(body: str) -> list[tuple[str, str]]:
    """Return deferred criteria whose issue checkbox is not yet checked."""
    try:
        items = deferred(body)
    except ValueError:
        return []
    checked = _CHECKED.findall(body)
    return [(name, text) for name, text in items if not any(done in text for done in checked)]


def cited_files(value: object) -> list[str]:
    """Split one citation field into pathnames, dropping line-range suffixes.

    ``a.tsx, b.tsx``, ``a.tsx; b.tsx`` and ``a.tsx and b.tsx`` name two files.
    ``path.rs:191-342,245-336`` names one file with two line ranges.
    """
    if not isinstance(value, str):
        return []
    files: list[str] = []
    for part in _CITATION_SPLIT.split(value.strip()):
        part = part.strip()
        if not part or (files and _RANGE_ONLY.match(part)):
            continue
        files.append(_LINE_SUFFIX.sub("", part))
    return files


def uncited(value: object, changed: Iterable[str]) -> list[str] | None:
    """Return cited files the PR does not change, or None when nothing is cited."""
    files = cited_files(value)
    if not files:
        return None
    paths = set(changed)
    return [path for path in files if path not in paths]


def failure_summary(failures: list[str], limit: int = FAILURE_LIMIT) -> str:
    """List every failure up to ``limit``, then say how many more remain."""
    shown = "; ".join(failures[:limit])
    extra = len(failures) - limit
    more = f"; and {extra} more" if extra > 0 else ""
    return f"{len(failures)} problem(s): {shown}{more}"


def deferred_note(body: str) -> str:
    """Name the criteria the approval leaves for post-merge verification."""
    try:
        items = deferred(body)
    except ValueError:
        return ""
    if not items:
        return ""
    lines = "\n".join(f"- {name}: {text}" for name, text in items)
    return (
        "\n\nVerify after merge (does not block approval; the issue stays open"
        f" until each box is checked):\n{lines}"
    )
