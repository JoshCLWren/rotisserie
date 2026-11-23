"""Ready-for-work label projection and label doctor policy (issue #191).

Pure and deterministic: callers pass the item's labels plus authoritative
facts (the controller frontier offer and the Neon lease holder) and get the
exact label changes back. The existing stage labels stay the next-step
vocabulary. This module owns only the claim labels and the legacy set; owner
and stage contradictions use the work policy's own normaliser so the doctor
never fights the existing reconcilers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import factory_work_policy as work

AVAILABLE = "factory:available"
CLAIMED = "factory:claimed"
REJECTED = "factory:claim-rejected"
CLAIM_LABELS = frozenset({AVAILABLE, CLAIMED, REJECTED})
LABEL_CLIENT = "label"
MERGE_CONFLICT = "factory:merge-conflict"

# Actions an outside agent can claim. Merge and post-merge verification stay
# with the factory: they need merge authorization and production evidence.
CLAIMABLE_ACTIONS = frozenset(
    {"implement", "review", "review-findings", "ci-repair", "merge-conflict"}
)

# A claim request the event handler has not answered yet looks exactly like a
# stale one, so the doctor leaves young requests alone.
CLAIM_REQUEST_GRACE_SECONDS = 600
REJECTED_TTL_SECONDS = 3600

# Explicit, never computed: each entry has no reference in Rotisserie or the
# adopter. Quoted spellings are matched separately and map to their inner name.
LEGACY_LABELS = frozenset(
    {
        "priority:%20high",
        "factory:revision",
        "factory:done",
        "factory:ci/review",
        "factory:test",
        "factory:in-review",
        "factory:ready-nominated",
        "factory:torque",
        "factory:chatgpt",
        "factory-23",
        "status-building",
        "factory:works-by-opencode-free-model-factory-23",
        "chatgpt:claimed",
        "ralph-status:ready",
        "ralph-status:todo",
        "ralph-status:needs-review",
        "ralph-priority:normal",
        "ralph-wave:0",
    }
)


def is_legacy(label: str) -> bool:
    return label in LEGACY_LABELS or _quoted(label) is not None


def _quoted(label: str) -> str | None:
    if len(label) > 2 and label.startswith('"') and label.endswith('"'):
        return label[1:-1]
    return None


def legacy_replacement(label: str) -> str | None:
    """The label a legacy one certainly means, or None to drop it.

    A quoted factory or Ralph label is dropped, never unquoted: restoring
    ``"factory:ready"`` or ``"factory:41"`` would invent a stage or an owner
    that no controller wrote.
    """
    inner = _quoted(label)
    if inner is None or inner.startswith(("factory", "ralph-")):
        return None
    return inner


@dataclass(frozen=True)
class Holder:
    """Who Neon says holds the item. ``kind`` is none, factory, external or unknown."""

    kind: str
    actor: str = ""
    client: str = ""
    claim_id: str = ""

    @property
    def label_claim(self) -> bool:
        return self.kind == "external" and self.client == LABEL_CLIENT


NO_HOLDER = Holder("none")
UNKNOWN_HOLDER = Holder("unknown")


@dataclass(frozen=True)
class Facts:
    number: int
    kind: str  # issue | pr
    labels: frozenset[str]
    holder: Holder
    offer_status: str | None = None  # candidate | held | None (not offered)
    offer_action: str | None = None
    claimed_age: int | None = None  # seconds since factory:claimed was added
    rejected_age: int | None = None


@dataclass(frozen=True)
class Plan:
    number: int
    kind: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    holds: tuple[str, ...] = ()

    @property
    def changes(self) -> bool:
        return bool(self.add or self.remove)

    def as_dict(self, before: Iterable[str]) -> dict[str, object]:
        start = sorted(set(before))
        after = sorted((set(start) - set(self.remove)) | set(self.add))
        return {
            "target": f"{self.kind}:{self.number}",
            "before": start,
            "after": after,
            "add": list(self.add),
            "remove": list(self.remove),
            "reasons": list(self.reasons),
            "holds": list(self.holds),
        }


@dataclass
class _Draft:
    labels: set[str]
    add: set[str] = field(default_factory=set)
    remove: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    holds: list[str] = field(default_factory=list)

    def drop(self, label: str, reason: str) -> None:
        if label in self.labels:
            self.labels.discard(label)
            self.remove.add(label)
            self.add.discard(label)
            self.reasons.append(reason)

    def put(self, label: str, reason: str) -> None:
        if label not in self.labels:
            self.labels.add(label)
            self.add.add(label)
            self.remove.discard(label)
            self.reasons.append(reason)


def available_wanted(facts: Facts, labels: set[str]) -> tuple[bool, str]:
    """Whether the frontier, Neon and the labels agree the item is open for a claim."""
    if facts.holder.kind == "unknown":
        return False, "Neon lease state unknown: availability withheld"
    if facts.holder.kind != "none":
        return False, "a lease holds this item"
    if labels & work.BLOCKED_LABELS:
        return False, "blocked"
    owner = work.owner_of(labels)
    if owner not in (None, "factory:unowned"):
        return False, f"owned by {owner}"
    if CLAIMED in labels:
        return False, "a claim is requested or held"
    if facts.offer_status != "candidate":
        return False, "the controller frontier does not offer it now"
    if facts.offer_action not in CLAIMABLE_ACTIONS:
        return False, f"action {facts.offer_action} is factory-only"
    return True, f"frontier offers {facts.offer_action}"


def plan(facts: Facts) -> Plan:
    """Exact label changes that bring one item to its trustworthy projection."""
    draft = _Draft(set(facts.labels))

    for label in sorted(facts.labels):
        if not is_legacy(label):
            continue
        replacement = legacy_replacement(label)
        draft.drop(label, f"remove obsolete label {label}")
        if replacement and replacement not in draft.labels:
            draft.put(replacement, f"{label} means {replacement}")

    managed = "factory" in draft.labels or bool(draft.labels & CLAIM_LABELS)
    active = sorted(
        label
        for label in draft.labels
        if work.OWNER_RE.fullmatch(label) and label != "factory:unowned"
    )
    if len(active) > 1:
        draft.holds.append("several active owners: " + ", ".join(active))

    stages = [label for label in work.STAGE_PRECEDENCE if label in draft.labels]
    if len(stages) > 1:
        for extra in stages[1:]:
            draft.drop(extra, f"{stages[0]} outranks {extra}; one stage at a time")

    if facts.kind == "issue" and MERGE_CONFLICT in draft.labels:
        draft.drop(MERGE_CONFLICT, "merge conflict is a pull-request state")

    if managed and len(active) <= 1 and "factory" in draft.labels:
        owner, stage = work.normalize_target_state(set(draft.labels), None)
        if owner != "factory:unowned" and "factory:unowned" in draft.labels:
            draft.drop("factory:unowned", f"{owner} owns it; unowned is stale")
        current = work.stage_of(draft.labels)
        if current is not None and stage != current:
            draft.drop(current, f"{current} contradicts owner {owner}")
            if stage is not None:
                draft.put(stage, f"normalized stage is {stage}")

    _claim_labels(facts, draft)

    return Plan(
        facts.number,
        facts.kind,
        tuple(sorted(draft.add)),
        tuple(sorted(draft.remove)),
        tuple(draft.reasons),
        tuple(draft.holds),
    )


def _claim_labels(facts: Facts, draft: _Draft) -> None:
    holder = facts.holder
    if holder.label_claim:
        draft.put(CLAIMED, f"Neon holds a label claim by {holder.actor}")
        draft.drop(REJECTED, "the claim was accepted")
    elif CLAIMED in draft.labels and holder.kind == "none":
        if facts.claimed_age is not None and facts.claimed_age >= CLAIM_REQUEST_GRACE_SECONDS:
            draft.drop(CLAIMED, "no Neon claim backs factory:claimed")
        else:
            draft.holds.append("claim request not answered yet")
    elif CLAIMED in draft.labels and holder.kind == "unknown":
        draft.holds.append("Neon unavailable: factory:claimed left as is")
    elif CLAIMED in draft.labels and holder.kind in ("factory", "external"):
        if facts.claimed_age is not None and facts.claimed_age >= CLAIM_REQUEST_GRACE_SECONDS:
            draft.drop(CLAIMED, "another lease holds the item; the label claim is not it")
        else:
            draft.holds.append("claim request not answered yet")

    if (
        REJECTED in draft.labels
        and facts.rejected_age is not None
        and facts.rejected_age >= REJECTED_TTL_SECONDS
    ):
        draft.drop(REJECTED, "rejection is older than an hour")

    wanted, why = available_wanted(facts, draft.labels)
    if wanted:
        draft.put(AVAILABLE, why)
    else:
        draft.drop(AVAILABLE, f"not available: {why}")


def facts_from_item(
    item: Mapping[str, object],
    *,
    holder: Holder,
    offer: Mapping[str, object] | None,
    claimed_age: int | None = None,
    rejected_age: int | None = None,
) -> Facts:
    return Facts(
        number=int(str(item["number"])),
        kind="pr" if item.get("pull_request") is not None or item.get("kind") == "pr" else "issue",
        labels=frozenset(work.labels_of(item)),
        holder=holder,
        offer_status=str(offer["status"]) if offer else None,
        offer_action=str(offer["action"]) if offer else None,
        claimed_age=claimed_age,
        rejected_age=rejected_age,
    )


def holder_from_lease(lease: Mapping[str, object] | None) -> Holder:
    """Decode a frontier lease row (operation ``external:actor/client``)."""
    if not lease:
        return NO_HOLDER
    operation = str(lease.get("operation") or "")
    if operation.startswith("external:"):
        actor, _, client = operation.removeprefix("external:").partition("/")
        return Holder("external", actor, client, str(lease.get("claim_id") or ""))
    return Holder("factory", str(lease.get("worker") or ""), "", str(lease.get("claim_id") or ""))
