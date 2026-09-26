"""Pure dependency-reference and executable-work eligibility domain."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Explicit prerequisite declaration pattern (generic; no host-specific labels/numbers).
DEP_ON_RE = re.compile(r"(?:[Dd]epends?\s+on)\s+([^\n]+)")
NUMBER_REF_RE = re.compile(r"#(\d+)")
DEPENDENCY_SEPARATORS = frozenset({"and", "&", "+", ","})

MANUAL_ONLY_MARKER = "<!-- factory-execution:manual-only -->"


@dataclass(frozen=True)
class DependencyDeclaration:
    """A single explicit prerequisite declaration in body text."""

    references: set[int]
    raw_text: str


def _leading_reference_numbers(text: str) -> set[int]:
    """Return issue numbers from the leading reference cluster only.

    Replicates the proven deterministic scanner: whitespace-separated
    tokens; references are added; separator tokens are skipped; first
    non-reference/non-separator token ends the cluster.
    """
    result: set[int] = set()
    for token in text.split():
        token = token.strip()
        if not token:
            continue
        ref = NUMBER_REF_RE.search(token)
        if ref:
            result.add(int(ref.group(1)))
        elif token.lower() not in DEPENDENCY_SEPARATORS:
            break
    return result


def parse_dependency_numbers(body: str) -> set[int]:
    """Return issue numbers declared as explicit prerequisites.

    Only ``Depends on #NNN`` / ``Depends on #N, #M`` / ``Depends on #N + #M``
    clusters count. Casual ``#N`` mentions elsewhere are ignored.
    """
    numbers: set[int] = set()
    for match in DEP_ON_RE.finditer(body):
        cluster_text = match.group(1)
        # Extract leading references only from the declaration tail.
        refs = _leading_reference_numbers(cluster_text)
        numbers.update(refs)
    return numbers


def has_unresolved_dependencies(body: str, open_numbers: set[int]) -> bool:
    """Return whether a prerequisite declared in *body* is still open.

    *open_numbers* is the set of currently unresolved prerequisite
    identifiers (pure domain; caller supplies from adapter/state layer).
    """
    declared = parse_dependency_numbers(body)
    return bool(declared & open_numbers)


def is_explicit_dependency_reference(body: str, number: int) -> bool:
    """Return whether *number* appears in an explicit ``Depends on`` cluster.

    Casual mentions of the same number elsewhere in the body are not
    counted as prerequisites.
    """
    declared = parse_dependency_numbers(body)
    return number in declared


def dependency_declarations(body: str) -> list[DependencyDeclaration]:
    """Return each explicit prerequisite declaration found in *body*."""
    declarations: list[DependencyDeclaration] = []
    for match in DEP_ON_RE.finditer(body):
        refs = _leading_reference_numbers(match.group(1))
        declarations.append(DependencyDeclaration(references=refs, raw_text=match.group(0)))
    return declarations
