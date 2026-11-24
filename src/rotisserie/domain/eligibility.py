"""Portable queue-eligibility rules over pre-decoded, provider-neutral signals.

This is the single canonical home for the deterministic rules that decide
whether one work node may enter autonomous implementation. It ports the shared
semantics the factory runtime keeps in ``scripts/factory_eligibility.py`` into Rotisserie's
domain, but it owns only the *rules*: it never parses GitHub payloads, labels,
HTML markers, issue bodies, or any other host- or product-specific encoding.

``evaluate_eligibility`` is the per-node source of truth for the stable
:class:`WorkBlock` vocabulary. The snapshot-level :func:`implementation_decision`
in ``rotisserie.domain.policy`` builds its per-node signal subset and delegates
here, so both report the same codes in one canonical order; it then layers only
the snapshot-derived and dynamic concerns (ranking, capacity, backpressure,
lease liveness) on top. A caller choosing between the two should start from
:func:`implementation_decision` when it holds a :class:`GraphSnapshot`, and from
``evaluate_eligibility`` when it has already decoded a single node's signals.

An adopter's translator layer is responsible for decoding its own vocabulary
(factory ownership labels, the manual-only HTML directive, dependency prose,
epic/PRD labels, acceptance-parent bodies, no-diff retry accounting, selector
metadata) into the structured :class:`EligibilitySignals` consumed here. The
rules then evaluate identically for every adopter.

The evaluation is pure, deterministic, and fails closed: openness, the
human-only boundary decision, and lease ownership must all be supplied
explicitly, every applicable exclusion is reported in canonical order, and a
work node is eligible only when no exclusion applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rotisserie.domain.model import WorkId, WorkState


class WorkBlock(StrEnum):
    """A stable reason why fresh implementation must not start.

    The members are listed in canonical evaluation order. ``CAPACITY`` and
    ``BACKPRESSURE`` are dynamic, snapshot-level gates applied by
    ``rotisserie.domain.policy`` on top of the per-node verdict.
    """

    NOT_OPEN = "not_open"
    PRODUCT_EXCLUSION = "product_exclusion"
    PARENT_WORK = "parent_work"
    HUMAN_BOUNDARY = "human_boundary"
    BLOCKED = "blocked"
    TERMINAL = "terminal"
    ACTIVE_LEASE = "active_lease"
    DEPENDENCY_INCOMPLETE = "dependency_incomplete"
    IMPLEMENTATION_EXISTS = "implementation_exists"
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"
    SELECTOR_METADATA_MISSING = "selector_metadata_missing"
    CAPACITY = "capacity"
    BACKPRESSURE = "backpressure"


@dataclass(frozen=True)
class EligibilitySignals:
    """Decoded, host-neutral inputs for one work node's eligibility.

    Every field is already translated out of its host or product encoding by an
    adopter translator. The domain never reconstructs label strings, marker
    text, or issue bodies from these values.

    To fail closed, the three fields that back ``AGENTS.md`` hard invariants
    have no default: a caller must state the lifecycle (``state``), the
    human-only boundary decision (``human_gate``), and lease ownership
    (``active_lease``) explicitly, so a forgotten field cannot silently pass a
    gate. The remaining optional signals default to "not excluded"; the
    translator owns asserting them.

    Attributes:
        work: The work node under review.
        state: The decoded lifecycle state; only ``OPEN`` work is executable.
        human_gate: A human-only boundary is present (for example the adopter's
            structured manual-only directive); autonomous execution is gated.
        active_lease: Another active lease already owns this node.
        product_excluded: The adopter marked this node a product exclusion
            (for example a registry, watchdog, or otherwise protected node).
        parent: The node is a parent or aggregation node (epic/PRD or a
            declared acceptance parent), not an executable leaf.
        blocked: A blocking signal is present (for example a blocked label).
        terminal: A terminal signal is present (for example a done label).
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
    human_gate: bool
    active_lease: bool
    product_excluded: bool = False
    parent: bool = False
    blocked: bool = False
    terminal: bool = False
    unresolved_dependencies: frozenset[WorkId] = frozenset()
    implementation_exists: bool = False
    retry_budget_exhausted: bool = False
    missing_selector_metadata: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EligibilityVerdict:
    """One work node's eligibility outcome with strictly parallel reasons.

    ``blocks`` carries the stable :class:`WorkBlock` codes in canonical
    evaluation order; ``reasons[i]`` is the human-readable explanation of
    ``blocks[i]``. Both tuples therefore always have the same length, and both
    are empty when the node is eligible. This mirrors ``WorkDecision.blocks`` so
    the two verdict shapes share one vocabulary.
    """

    work: WorkId
    eligible: bool
    blocks: tuple[WorkBlock, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.eligible != (not self.blocks):
            raise ValueError("eligibility must match the absence of blocks")
        if len(self.reasons) != len(self.blocks):
            raise ValueError("reasons must run parallel to blocks")


def evaluate_eligibility(signals: EligibilitySignals) -> EligibilityVerdict:
    """Decide whether a work node may enter autonomous implementation.

    Blocks are evaluated in :class:`WorkBlock`'s canonical order so verdicts are
    deterministic for identical signals, and every applicable block is reported
    with a parallel human-readable reason. The rule set fails closed: a node is
    eligible only when no block applies.
    """

    blocks: list[WorkBlock] = []
    reasons: list[str] = []

    def block(code: WorkBlock, reason: str) -> None:
        blocks.append(code)
        reasons.append(reason)

    if signals.state is not WorkState.OPEN:
        block(WorkBlock.NOT_OPEN, f"work is {signals.state.value}, not open")
    if signals.product_excluded:
        block(WorkBlock.PRODUCT_EXCLUSION, "work is a product exclusion")
    if signals.parent:
        block(WorkBlock.PARENT_WORK, "parent or aggregation node, not an executable leaf")
    if signals.human_gate:
        block(
            WorkBlock.HUMAN_BOUNDARY, "human-only boundary present: autonomous execution is gated"
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
            WorkBlock.IMPLEMENTATION_EXISTS, "an existing implementation already covers this work"
        )
    if signals.retry_budget_exhausted:
        block(
            WorkBlock.RETRY_BUDGET_EXHAUSTED,
            "retry budget exhausted: awaits new evidence, not a hand-authored result",
        )
    missing = sorted(signals.missing_selector_metadata)
    if missing:
        block(
            WorkBlock.SELECTOR_METADATA_MISSING, "missing selector metadata: " + ", ".join(missing)
        )

    return EligibilityVerdict(signals.work, not blocks, tuple(blocks), tuple(reasons))
