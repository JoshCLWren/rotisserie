"""Portable queue-eligibility rules over pre-decoded, provider-neutral signals.

This is the single canonical home for the deterministic rules that decide
whether one work node may enter autonomous implementation. It ports the shared
semantics ComicPile keeps in ``scripts/factory_eligibility.py`` into Rotisserie's
domain, but it owns only the *rules*: it never parses GitHub payloads, labels,
HTML markers, issue bodies, or any other host- or product-specific encoding.

An adopter's translator layer is responsible for decoding its own vocabulary
(factory ownership labels, the manual-only HTML directive, dependency prose,
epic/PRD labels, acceptance-parent bodies, no-diff retry accounting, selector
metadata) into the structured :class:`EligibilitySignals` consumed here. The
rules then evaluate identically for every adopter.

The evaluation is pure, deterministic, and fails closed: openness must be
supplied explicitly, every applicable exclusion is reported rather than only the
first, and a work node is eligible only when no exclusion applies.
"""

from __future__ import annotations

from dataclasses import dataclass

from rotisserie.domain.model import WorkId, WorkState
from rotisserie.domain.policy import WorkBlock


@dataclass(frozen=True)
class EligibilitySignals:
    """Decoded, host-neutral inputs for one work node's eligibility.

    Every field is already translated out of its host or product encoding by an
    adopter translator. The domain never reconstructs label strings, marker
    text, or issue bodies from these values.

    Attributes:
        work: The work node under review.
        state: The decoded lifecycle state; only ``OPEN`` work is executable.
        product_excluded: The adopter marked this node a product exclusion
            (for example a registry, watchdog, or otherwise protected node).
        parent: The node is a parent or aggregation node (epic/PRD or a
            declared acceptance parent), not an executable leaf.
        human_gate: A human-only boundary is present (for example the adopter's
            structured manual-only directive); autonomous execution is gated.
        blocked: A blocking signal is present (for example a blocked label).
        terminal: A terminal signal is present (for example a done label).
        active_lease: Another active lease already owns this node.
        unresolved_dependencies: Declared prerequisites that are not yet
            satisfied. Any self-reference is ignored.
        implementation_exists: An existing implementation already covers the
            node (for example an open canonical change).
        retry_budget_exhausted: A bounded retry budget is spent and the node
            must wait for new evidence rather than a hand-authored result.
        missing_selector_metadata: Selector-scoped metadata tokens that a
            specific selector requires but the node lacks. Empty for selectors
            that impose no such requirement.
    """

    work: WorkId
    state: WorkState
    product_excluded: bool = False
    parent: bool = False
    human_gate: bool = False
    blocked: bool = False
    terminal: bool = False
    active_lease: bool = False
    unresolved_dependencies: frozenset[WorkId] = frozenset()
    implementation_exists: bool = False
    retry_budget_exhausted: bool = False
    missing_selector_metadata: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EligibilityVerdict:
    """One work node's eligibility outcome with stable machine and human reasons.

    ``exclusions`` carries the stable :class:`WorkBlock` codes in canonical
    evaluation order; ``reasons`` carries parallel human-readable explanations.
    A node is eligible only when ``exclusions`` is empty.
    """

    work: WorkId
    eligible: bool
    exclusions: tuple[WorkBlock, ...]
    reasons: tuple[str, ...]


def evaluate_eligibility(signals: EligibilitySignals) -> EligibilityVerdict:
    """Decide whether a work node may enter autonomous implementation.

    Exclusions are evaluated in a fixed order so verdicts are deterministic for
    identical signals, and every applicable exclusion is reported. The rule set
    fails closed: a node is eligible only when no exclusion applies.
    """

    key = signals.work.key
    exclusions: list[WorkBlock] = []
    reasons: list[str] = []

    def block(code: WorkBlock, reason: str) -> None:
        exclusions.append(code)
        reasons.append(reason)

    if signals.state is not WorkState.OPEN:
        block(WorkBlock.NOT_OPEN, f"{key} is {signals.state.value}, not open")
    if signals.product_excluded:
        block(WorkBlock.PRODUCT_EXCLUSION, f"{key} is a product-excluded work node")
    if signals.parent:
        block(
            WorkBlock.PARENT_WORK,
            "parent or aggregation node, not an executable leaf",
        )
    if signals.human_gate:
        block(
            WorkBlock.HUMAN_BOUNDARY,
            "human-only boundary present: autonomous execution is gated",
        )
    if signals.blocked:
        block(WorkBlock.BLOCKED, "a blocking signal is present")
    if signals.terminal:
        block(WorkBlock.TERMINAL, "a terminal signal is present")
    if signals.active_lease:
        block(WorkBlock.ACTIVE_LEASE, "an active lease already owns this work")
    unresolved = sorted(
        dependency for dependency in signals.unresolved_dependencies if dependency != signals.work
    )
    if unresolved:
        block(
            WorkBlock.DEPENDENCY_INCOMPLETE,
            "unresolved dependencies: " + ", ".join(dependency.key for dependency in unresolved),
        )
    if signals.implementation_exists:
        block(
            WorkBlock.IMPLEMENTATION_EXISTS,
            "an existing implementation already covers this work",
        )
    if signals.retry_budget_exhausted:
        block(
            WorkBlock.RETRY_BUDGET_EXHAUSTED,
            "retry budget exhausted: awaits new evidence, not a hand-authored result",
        )
    missing = sorted(signals.missing_selector_metadata)
    if missing:
        block(
            WorkBlock.SELECTOR_METADATA_MISSING,
            "missing selector metadata: " + ", ".join(missing),
        )

    if not exclusions:
        reasons.append("eligible: no exclusion applies")
    return EligibilityVerdict(signals.work, not exclusions, tuple(exclusions), tuple(reasons))
