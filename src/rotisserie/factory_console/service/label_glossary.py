"""What each factory label means: the one glossary behind ``baste labels`` and both control rooms.

Every entry restates a rule from the factory runtime (``factory/.github/scripts``)
or ``factory-visibility.cjs``. ``tests/test_label_glossary.py`` reads the label
constants of those modules and fails when one has no entry here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from rotisserie.factory_console import runtime


@dataclass(frozen=True)
class Entry:
    name: str
    group: str
    meaning: str
    set_by: str
    cleared_by: str
    effect: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


_NO_WORKER = "The pool sends no worker to the item while it is on."
_PRIORITY = "Changes the pick-up order only."

_ROWS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "factory",
        "marker",
        "Factory work: the factory manages this item.",
        "A person on an issue; the factory on its own PRs; the PR enrollment reconciler.",
        "A person.",
        "The label doctor and the controllers change labels only on items with it. "
        "A PR on a branch outside factory/ enters the work queue only with it.",
    ),
    (
        "factory:<worker id>",
        "owner",
        "Factory worker N holds the next step.",
        "The worker when it takes the item.",
        "The worker's release, or the reconciler when the lease is stale.",
        "No other worker takes the item, and it is not offered for a claim.",
    ),
    (
        "factory:local",
        "owner",
        "The local factory session holds the next step.",
        "A local session when it takes the item.",
        "Its release, or the reconciler after the local lease expires (24 hours by default).",
        "No other worker takes the item, and it is not offered for a claim.",
    ),
    (
        "factory:unowned",
        "owner",
        "Factory work with no current owner.",
        "A worker's release; the controllers on new factory work.",
        "A worker that takes the item.",
        "Any worker can take the next step. It cannot stay in factory:building.",
    ),
    (
        "factory:building",
        "stage",
        "A factory worker implements or repairs the work.",
        "The worker that takes the item.",
        "The worker's push or release, which moves the stage.",
        "The label doctor drops it from an unowned item.",
    ),
    (
        "factory:review",
        "stage",
        "The current head needs review or re-review.",
        "The controllers after a push.",
        "The review controller, with its verdict.",
        "The next step is a review by a model that did not write the head.",
    ),
    (
        "factory:changes-requested",
        "stage",
        "Review findings block progress.",
        "The review controller on a change request.",
        "A repair push, which sends the head back to review.",
        "The next step is a review-findings repair.",
    ),
    (
        "factory:ci",
        "stage",
        "Review passed. The factory checks the required checks at the exact head.",
        "The review controller on an approval.",
        "The controllers when the checks pass or fail.",
        "The next step is CI repair when a required check fails.",
    ),
    (
        "factory:ready",
        "stage",
        "Every exact-head merge gate passes.",
        "The controllers when the last gate passes.",
        "The controllers; the merge closes the item.",
        "The pool sends no worker to it. It waits for the merge.",
    ),
    (
        "factory:blocked",
        "stage",
        "A human, credential or external blocker remains.",
        "A controller or a person; the migration lane; post-merge closure.",
        "A person, or the controller that set it, when the blocker is gone.",
        _NO_WORKER,
    ),
    (
        "factory:approved",
        "review",
        "Code review passed at this head. The merge gates still apply.",
        "The review controller after it validates an approval.",
        "The review controller when the stage goes back to building or changes-requested.",
        "Information only. It is not a stage and never grants a merge.",
    ),
    (
        "factory:merge-conflict",
        "derived",
        "GitHub reports the PR head as merge-conflicted.",
        "The PR state reconciler, from GitHub's mergeability.",
        "The same reconciler; the label doctor removes it from issues.",
        "The next step is merge-conflict work. It is never a stage.",
    ),
    (
        "factory:available",
        "claim",
        "Open for an outside claim on its next step.",
        "The label doctor only, from the controller frontier and the store.",
        "The label doctor, when a lease, an owner, a block or a claim appears.",
        "Add factory:claimed to claim it.",
    ),
    (
        "factory:claimed",
        "claim",
        "A claim is requested or held. The status comment names the claimant.",
        "A person or an outside agent.",
        "The claimant's finish; the label doctor 10 minutes after no store claim backs it.",
        "Bypasses the loop breaker, the split hold and incident PR closure.",
    ),
    (
        "factory:claim-rejected",
        "claim",
        "The last claim request was refused. The status comment says why.",
        "The label claim controller.",
        "An accepted claim, or the label doctor after one hour.",
        "Information only.",
    ),
    (
        "factory:override",
        "claim",
        "The owner picked this work, so it goes first.",
        "An accepted label claim; a claim hand-off; the owner.",
        "A person. No factory code removes it.",
        "Ranks ahead of all work except incident work. "
        "Bypasses the loop breaker and the split hold.",
    ),
    (
        "factory:parked",
        "state",
        "The loop breaker stopped sending workers at this head. See the Parked comment.",
        "The self-heal sweep; the label doctor.",
        "A new head, factory:override, a claim, incident work, or a targeted run.",
        "A mirror of the store park. While the park holds, no worker goes to the PR.",
    ),
    (
        "factory:needs-split",
        "state",
        "Too large for one factory task. The factory splits it.",
        "The dispatcher; the label doctor.",
        "The issue fits, or it gets factory:override or a claim.",
        "No implement run until the split. The split sweep sends one split run.",
    ),
    (
        "factory:gate-rejected",
        "state",
        "The acceptance gate rejected an approval at this head.",
        "The review controller; the self-heal sweep; the label doctor.",
        "A new head, or a gate pass at the head.",
        "A mirror of the store record.",
    ),
    (
        "factory:duplicate",
        "state",
        "Another open PR delivers the same issue. This one is newer.",
        "The self-heal sweep; the label doctor.",
        "One PR is left.",
        "The self-heal sweep keeps one PR of the group and closes the others.",
    ),
    (
        "factory:cooling",
        "state",
        "Dispatch backoff: retry cooling or review churn holds the PR. It is not a park.",
        "The self-heal sweep only.",
        "The hold expires.",
        "No worker until the cooling ends.",
    ),
    (
        "factory:incident-work",
        "state",
        "Work for an active incident. factory:incident on the issue declares it.",
        "The self-heal sweep; the label doctor.",
        "The incident ends.",
        "A mirror only. Incident work ranks first and is never parked.",
    ),
    (
        "factory:incident",
        "owner input",
        "The owner declared an incident on this issue.",
        "baste incident start, or the owner.",
        "Removing it ends the incident. The dispatcher removes it after the issue closes.",
        "Incident work ranks first. The issue skips intake limits. "
        "New intake of other issues pauses.",
    ),
    (
        "factory:allow-self-review",
        "owner input",
        "The owner lets the model that wrote this head review it.",
        "A person (a bot or GitHub Actions does not count).",
        "The drain sweep and the PR reconciler when the PR closes or its head changes.",
        "Covers one head. Every other gate still applies.",
    ),
    (
        "factory:migration-wait",
        "lane",
        "Another PR of the same migration family finishes first.",
        "The migration lane controller.",
        "The migration lane controller when the PR's turn comes.",
        "Comes with factory:blocked and factory:unowned, so no worker goes to it.",
    ),
    (
        "agent:*",
        "outside agent",
        "An outside agent works on the item (agent:NAME).",
        "The outside agent.",
        "The agent; the label doctor after 24 hours with no activity.",
        "The factory sends no worker, and the label doctor leaves the item alone.",
    ),
    (
        "via:*",
        "information",
        "The provider and model whose saved work is the current tip.",
        "The checkpoint hand-off.",
        "Replaced when another model's work becomes the tip.",
        "Information only.",
    ),
    (
        "ralph-status:blocked",
        "status",
        "Blocked, the same as factory:blocked.",
        "Post-merge closure; the migration lane.",
        "A person or the controller that set it.",
        _NO_WORKER,
    ),
    (
        "ralph-status:done",
        "status",
        "The work is done.",
        "The factory controllers.",
        "Post-merge closure when the issue reopens.",
        "The work policy clears its owner and stage. The pool skips the issue.",
    ),
    (
        "ralph-status:*",
        "status",
        "A status that some controllers keep beside the stage labels.",
        "The factory controllers.",
        "The factory controllers.",
        "The factory stage label decides the next step, not this one.",
    ),
    (
        "wontfix",
        "blocking",
        "GitHub default label. The factory reads it as blocked.",
        "A person.",
        "A person.",
        _NO_WORKER,
    ),
    (
        "invalid",
        "blocking",
        "GitHub default label. The factory reads it as blocked.",
        "A person.",
        "A person.",
        _NO_WORKER,
    ),
    (
        "duplicate",
        "blocking",
        "GitHub default label. The factory reads it as blocked.",
        "A person.",
        "A person.",
        _NO_WORKER,
    ),
    (
        "epic",
        "split",
        "A large body of work, not one task.",
        "A person.",
        "A person.",
        "The pool never sends an implement run to it, and the split sweep does not split it.",
    ),
    (
        "prd",
        "split",
        "A product requirements document, not one task.",
        "A person.",
        "A person.",
        "The pool never sends an implement run to it, and the split sweep does not split it.",
    ),
    (
        "umbrella",
        "split",
        "Too large for one task.",
        "A person; the factory on the second closed attempt of an issue.",
        "A person.",
        "No implement run. The factory adds factory:needs-split and splits the issue.",
    ),
    (
        "tracking",
        "split",
        "A parent that tracks other issues.",
        "A person; the split sweep on a parent it split.",
        "A person.",
        "No implement run. The factory adds factory:needs-split unless it already split it.",
    ),
    (
        "main-breakage",
        "priority",
        "Main is broken.",
        "Whoever files or triages the issue.",
        "A person.",
        "First lane by default. " + _PRIORITY,
    ),
    (
        "deploy-fix",
        "priority",
        "A fix for a failed deploy.",
        "Whoever files or triages the issue.",
        "A person.",
        "Same lane as main-breakage. " + _PRIORITY,
    ),
    (
        "user-reported",
        "priority",
        "A user reported it. With bug, it is a user bug.",
        "A person.",
        "A person.",
        "With bug, the user-bug lane. " + _PRIORITY,
    ),
    (
        "bug",
        "priority",
        "A defect. With user-reported, it is a user bug.",
        "Whoever files or triages the issue.",
        "A person.",
        "With user-reported, the user-bug lane. " + _PRIORITY,
    ),
    (
        "e2e-discovered",
        "priority",
        "An end-to-end run found it.",
        "Whoever files or triages the issue.",
        "A person.",
        "The e2e lane. " + _PRIORITY,
    ),
)

_INFRA: tuple[str, ...] = (
    "infrastructure",
    "e2e-infrastructure",
    "policy-change",
    "docs",
    "documentation",
    "quality-control",
)
_PRIORITIES: tuple[str, ...] = (
    "ralph-priority:critical",
    "priority:P0",
    "priority: P0",
    "ralph-priority:high",
    "priority:high",
    "priority: high",
    "ralph-priority:medium",
    "ralph-priority:low",
)

ENTRIES: tuple[Entry, ...] = (
    *(Entry(*row) for row in _ROWS),
    *(
        Entry(
            name,
            "priority",
            "Infrastructure work.",
            "A person.",
            "A person.",
            "The infrastructure lane, last by default. " + _PRIORITY,
        )
        for name in _INFRA
    ),
    *(
        Entry(
            name,
            "priority",
            "An explicit priority inside a lane.",
            "A person.",
            "A person.",
            "Higher priority goes first inside its lane. " + _PRIORITY,
        )
        for name in _PRIORITIES
    ),
)
LEGACY = Entry(
    "legacy",
    "legacy",
    "Obsolete: no Rotisserie or adopter code reads it.",
    "Older factory versions.",
    "The label doctor (--repair). --delete-legacy also deletes its definition.",
    "None.",
)
BY_NAME = {entry.name: entry for entry in ENTRIES}
WORKER_OWNER = re.compile(r"^factory:[1-9][0-9]*$")
PREFIXES = ("agent:", "via:", "ralph-status:")


def legacy_labels() -> list[str]:
    return sorted(runtime.module("factory_label_policy").LEGACY_LABELS)


def lookup(label: str) -> Entry | None:
    """The glossary entry for one label name, or None for a label the factory does not use."""
    if label in BY_NAME:
        return BY_NAME[label]
    if runtime.module("factory_label_policy").is_legacy(label):
        return LEGACY
    if WORKER_OWNER.fullmatch(label):
        return BY_NAME["factory:<worker id>"]
    prefix = next((p for p in PREFIXES if label.startswith(p) and label != p), None)
    return BY_NAME[prefix + "*"] if prefix else None


def glossary() -> dict[str, object]:
    return {
        "entries": [entry.to_dict() for entry in ENTRIES],
        "legacy": {**LEGACY.to_dict(), "labels": legacy_labels()},
    }
