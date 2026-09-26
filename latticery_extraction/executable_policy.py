"""Pure executable-work eligibility domain (no GitHub/network side effects)."""
from __future__ import annotations

from latticery_extraction.dependency_policy import (
    MANUAL_ONLY_MARKER,
    has_unresolved_dependencies,
)


def is_executable(
    body: str,
    open_prerequisites: set[int],
    manual_only_true: bool = False,
) -> bool:
    """Return whether work described in *body* is eligible for execution.

    Rules (pure, deterministic):
    - Explicit unresolved prerequisites block execution.
    - Manual-only marker blocks autonomous execution.
    - No dependencies and not manual-only => executable.

    *open_prerequisites* is the caller-provided set of unresolved
    prerequisite identifiers; this keeps the function free of network
    or process state.
    """
    if manual_only_true or MANUAL_ONLY_MARKER in body:
        return False
    if has_unresolved_dependencies(body, open_prerequisites):
        return False
    return True


def eligibility_reason(body: str, open_prerequisites: set[int]) -> str | None:
    """Return a deterministic reason if not executable, else None."""
    if MANUAL_ONLY_MARKER in body:
        return "manual-only marker present"
    # Re-use pure dependency scanner without importing internals unnecessarily.
    from latticery_extraction.dependency_policy import parse_dependency_numbers

    declared = parse_dependency_numbers(body)
    unresolved = declared & open_prerequisites
    if unresolved:
        return f"unresolved prerequisites: {sorted(unresolved)}"
    return None
