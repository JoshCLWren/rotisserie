#!/usr/bin/env python3
"""Fail when autonomous factory policy files drift on critical invariants."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "docs/AUTONOMOUS_FACTORY_POLICY.md"
PROTOCOL = ROOT / "docs/ISSUE_EXECUTION_PROTOCOL.md"
SCHEDULED_PROMPT = ROOT / "docs/CHATGPT_FACTORY_PROMPT.md"
ENTRYPOINT = ROOT / "scripts/comic-pile-opencode-factory.sh"
HEARTBEAT_ENTRYPOINT = ROOT / "scripts/comic-pile-opencode-factory-heartbeat.sh"
NEXT_TASK_PROMPT = ROOT / "prompts/agent-next-task.md"
ISSUE_SKILL = ROOT / ".agents/skills/github-issue-kanban/SKILL.md"
LEGACY_PIPELINE = ROOT / "scripts/opencode_pipeline.sh"


def require(text: str, needle: str, source: Path) -> None:
    """Require one invariant string in a policy source."""
    if needle in {"Version: 22", "FACTORY POLICY V22", "Version: 23", "FACTORY POLICY V23", "Version: 24", "FACTORY POLICY V24"}:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])"
        present = re.search(pattern, text) is not None
    else:
        present = needle in text
    if not present:
        raise SystemExit(f"{source}: missing required policy text: {needle!r}")


def forbid(text: str, needle: str, source: Path) -> None:
    """Reject one known contradictory policy string."""
    if needle in text:
        raise SystemExit(f"{source}: forbidden policy drift found: {needle!r}")


def forbid_marker(text: str, marker: str, source: Path) -> None:
    """Reject a complete version marker without substring false positives."""
    pattern = rf"(?<![A-Za-z0-9]){re.escape(marker)}(?![A-Za-z0-9])"
    if re.search(pattern, text):
        raise SystemExit(f"{source}: forbidden policy marker found: {marker!r}")


def require_order(text: str, before: str, after: str, source: Path) -> None:
    """Require two policy phrases to appear in their canonical order."""
    before_index = text.find(before)
    after_index = text.find(after)
    if before_index < 0 or after_index < 0 or before_index >= after_index:
        raise SystemExit(
            f"{source}: policy order requires {before!r} before {after!r}"
        )


def validate_texts(policy: str, protocol: str, entrypoint: str) -> None:
    """Validate factory control-plane texts against canonical invariants."""
    for needle in (
        "Version: 24",
        "ruff check .",
        "ty check --error-on-warning",
        "Path-filtered ruff or ty is not a valid CI substitute",
        "Drive the open issue backlog to zero",
        "Factory ownership is a connection-pool lock around the next action",
        "Cross-worker takeover and merge are allowed",
        "release the lease when active work stops",
        "A controller claim is not durable until the worker reaches an executable session",
        "Exact-head review assignments use the multi-provider Entry path",
        "FACTORY_OMNIROUTE_ENABLED=off",
        "do not restore the temporary `auto/best-free` capacity bridge",
        "truthful no-work completion",
        "The highest-priority unclaimed open issue labeled both `user-reported` and `bug`",
        "The highest-priority unclaimed reproducible E2E-discovered product `bug` issue",
        "When fewer than four substantive implementation PRs are open",
        "limited to at most three concurrent in-flight units",
        "At most one implementation worker may own an issue",
        "Existing open PRs are not automatically higher priority than unclaimed issues.",
        "Release notes are asynchronous post-merge infrastructure",
        "Implementation workers must not create, repair, or require `docs/changelog.d` fragments",
        "fetch review submissions and all current inline review threads",
        "A worker's own review conclusion does not silently override existing human or bot feedback.",
        "Workers may merge a PR without asking again only after all of these gates are satisfied",
        "the worker supplies the exact expected head SHA",
        "Formal GitHub `APPROVED` state is not a required merge gate",
        "Never enable auto-merge.",
        "Never push directly to `main`.",
        "Never create or convert a draft PR unless Josh explicitly requests a draft.",
        "## Durable resume packet",
        "<!-- factory-resume:v1 -->",
        "one full label-set replacement",
        "Never implement a transition as separate remove-then-add calls",
        "Heartbeat telemetry never counts as substantive progress",
        "registry issue #1093",
        "Issues labeled `epic` or `prd` are not ordinary autonomous factory candidates.",
        "<!-- factory-execution:manual-only -->",
        "Integrated parent product acceptance is human/interactive controlled.",
    ):
        require(policy, needle, POLICY)

    forbid_marker(policy, "Version: 21", POLICY)
    forbid_marker(policy, "Version: 220", POLICY)
    forbid(policy, "#679", POLICY)
    forbid(policy, "backlog-zero checkpoint", POLICY)
    require(policy, "within equal priority, choose the newest report first", POLICY)

    for marker in (
        "comic-pile-factory-implement-claim-v3",
        "comic-pile-factory-implement-progress-v3",
        "comic-pile-factory-review-claim-v2",
        "comic-pile-factory-review-v2:<sha>:pass",
        "changes-required",
        "comic-pile-factory-fix-claim-v3",
        "comic-pile-factory-fix-progress-v3",
        "comic-pile-factory-ready-v2",
        "factory-resume:v1",
        "comic-pile-factory-needs-human-v2",
        "comic-pile-factory-claim-released-v3",
    ):
        require(policy, marker, POLICY)

    for obsolete in (
        "## User-facing changelog gate",
        "HONEST STAGE FAST PATH",
        "Planning PRs are encouraged",
        "Always split large PRs into stages",
        "Prefer finishing already-started issues over starting new ones.",
        "Do not start a new issue while an owned issue has executable remaining work.",
        "Never merge.",
        "full configured E2E matrix",
    ):
        forbid(policy, obsolete, POLICY)

    for needle in (
        "docs/AUTONOMOUS_FACTORY_POLICY.md",
        "daily Chromium discovery",
        "Discovery failures must preserve traces, screenshots, video, JSON results, backend logs, and run metadata",
        "Reproducible product failures become focused `bug` issues",
        "Ordinary factories do not launch the complete discovery suite",
        "Never create a draft pull request unless Josh explicitly requests a draft.",
        "Before pass, readiness, or merge, inspect the exact current head SHA",
        "An unresolved actionable correctness, security, ownership, data-integrity, migration, concurrency, recovery, or test-validity finding blocks readiness and merge.",
        "green on every required check",
        "free of unresolved actionable review findings",
        "The merge operation must include the exact expected head SHA.",
        "Never enable auto-merge.",
        "Autonomous factory workers may merge",
        "ruff check .",
        "ty check --error-on-warning",
        "Path-filtered ruff or ty is not a valid CI substitute",
        "Autonomous factories must not claim issues labeled `epic` or `prd`",
        "<!-- factory-execution:manual-only -->",
        "Integrated parent product acceptance is human/interactive controlled.",
    ):
        require(protocol, needle, PROTOCOL)
    forbid(protocol, "#679", PROTOCOL)

    for needle in (
        "docs/AUTONOMOUS_FACTORY_POLICY.md",
        "Drive the open issue backlog to zero",
        "Release notes are post-merge infrastructure",
        "database-backed release ledger",
        "highest-priority unclaimed open issue labeled both `user-reported` and `bug`",
        "reproducible E2E-discovered product bugs",
        "full maintained Chromium discovery suite is an independent daily workflow",
        "preserves traces, screenshots, video, JSON results, backend logs, and run metadata",
        "Never launch the full discovery suite merely because your current work pool is empty",
        "record a truthful no-work completion",
        "factory:unowned",
        "exact expected head SHA",
        "Never enable auto-merge",
        "Never treat an empty or blocked backlog as a reason to self-pause or self-disable.",
        "Only Josh, or an interactive session acting on Josh's direct instruction, may pause or disable this factory.",
    ):
        require(entrypoint, needle, ENTRYPOINT)

    for obsolete in (
        "Treat the generated changelog as part of the completion contract",
        "docs/changelog.d/YYYY-MM-DD-<pr-number>.md",
        "Changelog: not user-facing",
        "open a truthful draft PR",
        "mark a draft ready when",
        "HONEST STAGE FAST PATH",
        "Prefer finishing already-started issues over starting new ones.",
        "Do not start a new issue while an owned issue has executable remaining work.",
        "Never merge.",
        "merge the pull request after CI",
        "ignore unresolved review threads",
        "Firefox + WebKit + Chromium",
        "core.hooksPath=/dev/null",
        "commit even if tests are not fully passing",
        "commit even if not fully passing",
    ):
        forbid(entrypoint, obsolete, ENTRYPOINT)


def validate_local_guidance() -> None:
    """Validate local and scheduled factory entry points independently."""
    for source in (SCHEDULED_PROMPT, HEARTBEAT_ENTRYPOINT, NEXT_TASK_PROMPT, ISSUE_SKILL):
        text = source.read_text(encoding="utf-8")
        require(text, "factory-resume:v1", source)

    scheduled = SCHEDULED_PROMPT.read_text(encoding="utf-8")
    require(scheduled, "Version: 24", SCHEDULED_PROMPT)
    require(scheduled, "FACTORY POLICY V24", SCHEDULED_PROMPT)
    forbid_marker(scheduled, "Version: 21", SCHEDULED_PROMPT)
    forbid_marker(scheduled, "Version: 220", SCHEDULED_PROMPT)
    forbid_marker(scheduled, "FACTORY POLICY V21", SCHEDULED_PROMPT)
    forbid_marker(scheduled, "FACTORY POLICY V220", SCHEDULED_PROMPT)
    forbid(scheduled, "#679", SCHEDULED_PROMPT)
    require(scheduled, "Release notes are post-merge infrastructure", SCHEDULED_PROMPT)
    require(scheduled, "one full atomic label-set replacement", SCHEDULED_PROMPT)
    require(scheduled, "At the start of every scheduled run", SCHEDULED_PROMPT)
    require(scheduled, "Heartbeat telemetry never counts as substantive progress", SCHEDULED_PROMPT)
    require(scheduled, "independent daily workflow", SCHEDULED_PROMPT)
    require(scheduled, "FROZEN-SCOPE GOVERNANCE.", SCHEDULED_PROMPT)
    require(scheduled, "<!-- factory-execution:manual-only -->", SCHEDULED_PROMPT)

    next_task = NEXT_TASK_PROMPT.read_text(encoding="utf-8")
    require(next_task, "only\n  after the PR merges", NEXT_TASK_PROMPT)
    require(next_task, "ruff check .", NEXT_TASK_PROMPT)
    require(next_task, "ty check --error-on-warning", NEXT_TASK_PROMPT)
    require(
        next_task,
        "Path-filtered ruff or ty is not a valid CI substitute",
        NEXT_TASK_PROMPT,
    )

    issue_skill = ISSUE_SKILL.read_text(encoding="utf-8")
    require(issue_skill, "After the PR merges", ISSUE_SKILL)
    require(issue_skill, "ruff check .", ISSUE_SKILL)
    require(issue_skill, "ty check --error-on-warning", ISSUE_SKILL)
    require(
        issue_skill,
        "Path-filtered ruff or ty is not a valid CI substitute",
        ISSUE_SKILL,
    )

    legacy = LEGACY_PIPELINE.read_text(encoding="utf-8")
    forbid(legacy, "core.hooksPath=/dev/null", LEGACY_PIPELINE)
    forbid(legacy, "commit even if tests are not fully passing", LEGACY_PIPELINE)
    forbid(legacy, "commit even if not fully passing", LEGACY_PIPELINE)


def read_entrypoint_text() -> str:
    """Read local orchestration prompts and the scheduled ChatGPT prompt template."""
    return "\n".join(
        (
            ENTRYPOINT.read_text(encoding="utf-8"),
            HEARTBEAT_ENTRYPOINT.read_text(encoding="utf-8"),
            SCHEDULED_PROMPT.read_text(encoding="utf-8"),
        )
    )


def main() -> None:
    """Read checked-in policy sources and validate their alignment."""
    validate_texts(
        POLICY.read_text(encoding="utf-8"),
        PROTOCOL.read_text(encoding="utf-8"),
        read_entrypoint_text(),
    )
    validate_local_guidance()
    print("Autonomous factory policy invariants are aligned.")


if __name__ == "__main__":
    main()
