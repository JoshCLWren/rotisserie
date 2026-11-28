#!/usr/bin/env python3
"""Shared executable-eligibility semantics for factory work selectors.

This module is the single canonical home for the pure eligibility rules used
by both work-selection paths:

- ``scripts/next_task.py`` — the local/Ralph issue selector, and
- ``.github/scripts/factory_work_policy.py`` — the fixed-model controller.

Both selectors must agree on what makes an issue executable. Historically each
carried its own copy of the epic/PRD, manual-only, dependency, blocked-label,
and ownership rules, and the copies drifted. Import from here instead of
reimplementing the rules.

Manual-only directive contract
------------------------------
Autonomous execution is excluded by either of these standalone lines, outside
fenced code blocks and inline code spans::

    <!-- factory-execution:manual-only -->
    manual-only

Raw substring matching is explicitly NOT the contract: merely discussing the
marker in prose or quoting it in code (for example, in backticks) must never
exclude an issue. To mention the marker without triggering exclusion, wrap it
in code spans or place it inside a sentence.

Dependency declaration contract
-------------------------------
Explicit prerequisites are declared in exactly two structured forms:

1. An inline ``Depends on #N[, #M ...]`` cluster. Only the leading reference
   cluster counts: consecutive ``#N`` tokens joined by ``and``/``,``/``+``.
   Prose or casual ``#N`` mentions later on the line end the cluster.
2. A dependency heading (``## Dependencies``, ``Depends on:``,
   ``Blocked by``, ``Prerequisites``) followed by list items carrying
   ``#N`` references, one per item.

Markdown wrapping (backticks, bold, ``[#N](url)`` links) around a reference
never hides a declaration: formatting is normalized before extraction.

``ralph-task`` / ``ralph-status:pending`` scope
----------------------------------------------
Those two labels are Ralph-selector metadata, not universal execution
metadata. ``scripts/next_task.py`` requires them because it serves the local
Ralph queue. The fixed-model controller (which owns fixed-model dispatch)
never requires them and selects on factory labels instead. See
``docs/FACTORY_QUEUE_ELIGIBILITY.md`` for the authoritative role table.

Comment trust
-------------
Marker trust is not an eligibility rule and is deliberately not duplicated
here. ``factory_work_policy.comment_is_trusted`` is the single gate used by
every lease-activity, no-diff, and strike-reset scan: it accepts a trusted
``OWNER``/``MEMBER``/``COLLABORATOR`` association or a workflow-posted comment
(``performed_via_github_app.slug == github-actions``), matching
``docs/FACTORY_GITHUB_VISIBILITY.md``. Eligibility itself reads issue state,
labels, and body only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import _rotisserie_native

MANUAL_ONLY_MARKER = "<!-- factory-execution:manual-only -->"

def nonclosing_issue_numbers(body: str | None) -> set[int]:
    """Find issue numbers explicitly denied closure or declared as partial."""
    return _rotisserie_native.nonclosing_issue_numbers(body)


def explicit_closing_issue(body: str | None) -> int | None:
    """Honor affirmative closing references, never negations or partial work."""
    return _rotisserie_native.explicit_closing_issue(body)


ACCEPTANCE_PARENT_BODY_RE = re.compile(
    r"acceptance parent|parent acceptance criteria",
    re.IGNORECASE,
)
ACCEPTANCE_PARENT_CHILD_RE = re.compile(r"(?m)^[ \t]*-[ \t]*\[[ xX]\][ \t]*#\d+")

EPIC_PRD_LABELS = frozenset({"epic", "prd"})
BLOCKED_LABELS = frozenset(
    {"factory:blocked", "ralph-status:blocked", "wontfix", "invalid", "duplicate"}
)
# Mirrors ``issue_is_static_candidate``: ``ralph-status:done`` is the terminal
# task label and ``factory:ready`` is the terminal workflow label, so neither
# may be reported as queue-eligible by the diagnostic.
TERMINAL_LABELS = frozenset({"ralph-status:done", "factory:ready"})

FACTORY_OWNER_RE = re.compile(r"^factory:(?:unowned|local|[1-9][0-9]*)$")

def strip_code_segments(text: str) -> str:
    """Remove fenced code blocks and inline code spans from markdown text."""
    return _rotisserie_native.strip_code_segments(text)


def is_manual_only(body: str | None) -> bool:
    """Return whether a body carries a structured manual-only opt-out.

    Either a standalone ``<!-- factory-execution:manual-only -->`` comment or
    a standalone ``manual-only`` line counts, and only outside fenced code
    blocks and inline code spans. Prose discussion and quoted mentions never
    trigger exclusion.

    Args:
        body: The raw issue body, or None for an empty body.

    Returns:
        True when autonomous execution is explicitly disallowed.
    """
    return _rotisserie_native.is_manual_only(body)


def is_acceptance_parent(body: str | None) -> bool:
    """Return whether a body declares a product-acceptance parent contract.

    Both halves are required: the body must present itself as the acceptance
    parent and declare a checkbox child graph, so ordinary implementation
    issues that merely defer an operator acceptance pass stay executable.

    Args:
        body: The raw issue body, or None for an empty body.

    Returns:
        True when the body declares a parent acceptance contract.
    """
    text = body or ""
    return bool(
        ACCEPTANCE_PARENT_BODY_RE.search(text) and ACCEPTANCE_PARENT_CHILD_RE.search(text)
    )


def parse_inline_depends_on_numbers(body: str) -> set[int]:
    """Return issue numbers from inline dependency declarations."""
    return _rotisserie_native.parse_inline_depends_on_numbers(body)


def parse_heading_dependency_numbers(body: str) -> set[int]:
    """Return issue numbers from list items under a dependency heading."""
    return _rotisserie_native.parse_heading_dependency_numbers(body)


def parse_declared_dependencies(body: str | None) -> set[int]:
    """Return every issue number declared as an explicit prerequisite.

    Unions the inline ``Depends on #N`` cluster form and the
    heading-plus-list form. Casual ``#N`` mentions elsewhere never count.

    Args:
        body: The raw issue body, or None for an empty body.

    Returns:
        The set of declared prerequisite issue numbers.
    """
    return _rotisserie_native.parse_declared_dependencies(body)


def owner_of(labels: Iterable[str]) -> str | None:
    """Return the active factory owner represented by a label set."""
    owners = [label for label in labels if FACTORY_OWNER_RE.fullmatch(label)]
    active = [label for label in owners if label != "factory:unowned"]
    if active:
        return sorted(active)[0]
    return "factory:unowned" if "factory:unowned" in owners else None


def has_active_factory_owner(labels: Iterable[str]) -> bool:
    """Return whether a label set carries an active factory lease owner."""
    owner = owner_of(labels)
    return owner is not None and owner != "factory:unowned"


def is_factory_unowned(labels: Iterable[str]) -> bool:
    """Return whether a label set has no active factory owner."""
    return owner_of(labels) in (None, "factory:unowned")


@dataclass(frozen=True)
class EligibilityVerdict:
    """One issue's queue-eligibility outcome with stable human reasons."""

    eligible: bool
    reasons: tuple[str, ...]


def explain_issue_eligibility(
    *,
    number: int,
    state: str,
    labels: set[str],
    body: str | None,
    open_numbers: set[int],
    suppressing_issue_numbers: set[int] | None = None,
    non_executable_numbers: set[int] | None = None,
    no_diff_attempts: int = 0,
    no_diff_limit: int = 3,
    require_ralph_labels: bool = False,
) -> EligibilityVerdict:
    """Explain why one issue is or is not currently queue-eligible.

    This covers the static, selector-independent share of eligibility: state,
    product exclusions, the structured manual-only directive, acceptance
    parents, blocked/terminal labels, ownership, explicit dependencies,
    and canonical open-PR suppression. No-diff retries never suppress an
    issue (#90); the counts are accepted for caller compatibility. Dynamic
    gates (worker WIP caps, review backpressure, live GitHub blockers, lease
    liveness) are evaluated by the calling selector and appended separately.

    Args:
        number: The issue number under review.
        state: The GitHub issue state (``OPEN`` means visible).
        labels: The issue's normalized label names.
        body: The raw issue body, or None.
        open_numbers: Numbers of currently open issues for dependency checks.
        suppressing_issue_numbers: Issues owning an open canonical PR.
        non_executable_numbers: Product-excluded issue numbers (registry, …).
        no_diff_attempts: Consumed no-diff retries inside the rolling window.
        no_diff_limit: Retries allowed before suppression.
        require_ralph_labels: When True, also require the Ralph-selector
            metadata (``ralph-task`` + ``ralph-status:pending``).

    Returns:
        The eligibility verdict with human-readable reasons.
    """
    reasons: list[str] = []
    eligible = True

    def block(reason: str) -> None:
        nonlocal eligible
        eligible = False
        reasons.append(reason)

    if state.upper() != "OPEN":
        block(f"#{number} is {state.lower()}, not open")
    if non_executable_numbers and number in non_executable_numbers:
        block(f"#{number} is a non-executable product exclusion")
    if labels & set(EPIC_PRD_LABELS):
        block("labeled epic/prd: parent product work, not ordinary implementation")
    if is_manual_only(body):
        block("structured manual-only directive present: human/interactive gate")
    if is_acceptance_parent(body):
        block("body declares a product-acceptance parent contract")
    blocked = sorted(labels & set(BLOCKED_LABELS))
    if blocked:
        block(f"blocked labels present: {', '.join(blocked)}")
    terminal = sorted(labels & set(TERMINAL_LABELS))
    if terminal:
        block(f"terminal label present: {', '.join(terminal)}")
    owner = owner_of(labels)
    if owner is not None and owner != "factory:unowned":
        block(f"active factory lease held by {owner}")
    declared = parse_declared_dependencies(body)
    unresolved = sorted(declared & (open_numbers - {number}))
    if unresolved:
        block(
            "unresolved dependencies: "
            + ", ".join(f"#{entry}" for entry in unresolved)
        )
    if suppressing_issue_numbers and number in suppressing_issue_numbers:
        block("an open canonical PR already owns this issue")
    if require_ralph_labels:
        if "ralph-task" not in labels:
            block("missing Ralph-selector metadata: ralph-task")
        if "ralph-status:pending" not in labels:
            block("missing Ralph-selector metadata: ralph-status:pending")

    if eligible:
        reasons.append("eligible: no static exclusion applies")
    return EligibilityVerdict(eligible=eligible, reasons=tuple(reasons))
