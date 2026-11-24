"""Label doctor: audit and repair ready-for-work and lifecycle labels (issue #191).

The factory store and the controller frontier decide; labels are projections. The doctor
reads each open factory item once, plans its exact changes with the pure
factory label policy, and writes only per-label additions and removals after
re-reading the item, so a label someone adds meanwhile is never overwritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Any
from urllib.parse import quote

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubNotFoundError,
    obj,
    string,
)
from rotisserie.factory_console.service import external, frontier

LIST_CAP = 3000


def policy() -> ModuleType:
    return runtime.module("factory_label_policy")


@dataclass
class Report:
    repository: str
    generated_at: str
    mode: str
    authority: str
    items: list[dict[str, object]] = field(default_factory=list)
    holds: list[dict[str, object]] = field(default_factory=list)
    applied: list[dict[str, object]] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)
    legacy_definitions: list[str] = field(default_factory=list)
    deleted_definitions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "authority": self.authority,
            "changes": self.items,
            "holds": self.holds,
            "applied": self.applied,
            "skipped": self.skipped,
            "legacy_definitions": self.legacy_definitions,
            "deleted_definitions": self.deleted_definitions,
        }


def _relevant(lp: ModuleType, labels: set[str], offered: bool) -> bool:
    if offered or "factory" in labels or labels & lp.CLAIM_LABELS:
        return True
    return any(label.startswith("factory") or lp.is_legacy(label) for label in labels)


def open_items(github: GitHub) -> list[dict[str, Any]]:
    """Every open issue and PR in one paginated read (the issues endpoint lists both)."""
    pages = json.loads(
        github.gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{github.repo}/issues?state=open&per_page=100",
        )
    )
    rows = [
        row
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, list)
        for row in page
        if isinstance(row, dict)
    ]
    if len(rows) >= LIST_CAP:
        raise ValueError("open issue listing reached its cap; the doctor needs a complete view")
    return rows


def label_age(
    github: GitHub, number: int, label: str, now: datetime, updated_at: str = ""
) -> int | None:
    """Seconds since ``label`` was last added to the item, or None when unknown.

    GitHub stops listing events on a busy item, so the latest label add can be
    missing. The item's last update is never older than that add, which makes
    ``now - updated_at`` a safe lower bound for the label's age.
    """
    try:
        pages = json.loads(
            github.gh(
                "api",
                "--paginate",
                "--slurp",
                f"repos/{github.repo}/issues/{number}/events?per_page=100",
            )
        )
    except GitHubError, ValueError:
        return None
    stamps = [
        event.get("created_at")
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, list)
        for event in page
        if isinstance(event, dict)
        and event.get("event") == "labeled"
        and isinstance(event.get("label"), dict)
        and event["label"].get("name") == label
    ]
    if not stamps or not isinstance(stamps[-1], str):
        if not updated_at:
            return None
        stamps = [updated_at]
    added = datetime.fromisoformat(str(stamps[-1]).replace("Z", "+00:00"))
    return max(0, int((now - added).total_seconds()))


def audit(
    github: GitHub,
    *,
    now: datetime | None = None,
    snapshot: frontier.Frontier | None = None,
    items: list[dict[str, Any]] | None = None,
) -> tuple[Report, list[tuple[dict[str, Any], Any]]]:
    """Plan every open item's repair without writing anything."""
    lp = policy()
    instant = now or datetime.now(UTC)
    snap = snapshot or frontier.capture(github, now=instant)
    rows = items if items is not None else open_items(github)
    leases, problem = frontier._active_leases(github.repo)
    neon_known = snap.authoritative_claim_available and not problem
    offers = {offer.target: offer for offer in snap.offers}
    report = Report(
        github.repo,
        instant.isoformat(),
        "dry-run",
        "Factory store and controller frontier" if neon_known else (problem or snap.authority_note),
    )
    plans: list[tuple[dict[str, Any], Any]] = []
    core = runtime.module("factory_work_policy")
    for row in rows:
        number = int(row["number"])
        kind = "pr" if "pull_request" in row else "issue"
        labels = {
            str(label.get("name")) for label in row.get("labels") or [] if isinstance(label, dict)
        }
        offer = offers.get(f"{kind}:{number}")
        if not _relevant(lp, labels, offer is not None):
            continue
        if core.eligibility.is_manual_only(string(row, "body")):
            # The controllers never write labels on a manual-only hold.
            report.holds.append({"target": f"{kind}:{number}", "holds": ["manual-only"]})
            continue
        if not neon_known:
            holder = lp.UNKNOWN_HOLDER
        elif offer is not None and offer.lease is not None:
            holder = lp.holder_from_lease(offer.lease)
        else:
            holder = lp.holder_from_lease(leases.get(number))
        claimed_age = (
            label_age(github, number, lp.CLAIMED, instant, string(row, "updated_at"))
            if lp.CLAIMED in labels
            else None
        )
        rejected_age = (
            label_age(github, number, lp.REJECTED, instant, string(row, "updated_at"))
            if lp.REJECTED in labels
            else None
        )
        status = offer.status if offer else None
        if (
            offer is not None
            and status == "candidate"
            and holder.kind == "none"
            and offer.action in lp.CLAIMABLE_ACTIONS
            and lp.CLAIMED not in labels
        ):
            # The frontier does not read check state; reserve-next does. A PR
            # whose checks are still running would only collect rejections.
            live, _ = live_now(github, offer.target, offer.action, offer.head_sha)
            status = "candidate" if live else "held"
        facts = lp.Facts(
            number,
            kind,
            frozenset(labels),
            holder,
            status,
            offer.action if offer else None,
            claimed_age,
            rejected_age,
        )
        result = lp.plan(facts)
        if result.changes:
            report.items.append(result.as_dict(labels))
            plans.append((row, result))
        elif result.holds:
            report.holds.append({"target": f"{kind}:{number}", "holds": list(result.holds)})
    report.legacy_definitions = legacy_definitions(github)
    return report, plans


def live_now(github: GitHub, target: str, action: str, head: str | None) -> tuple[bool, str]:
    try:
        return external.live_now(github, target, action, head)
    except (GitHubError, ValueError) as exc:
        return False, str(exc)


def legacy_definitions(github: GitHub) -> list[str]:
    lp = policy()
    pages = json.loads(
        github.gh("api", "--paginate", "--slurp", f"repos/{github.repo}/labels?per_page=100")
    )
    names = [
        string(row, "name")
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, list)
        for row in page
        if isinstance(row, dict)
    ]
    return sorted(name for name in names if lp.is_legacy(name))


def _fresh_labels(github: GitHub, number: int) -> set[str]:
    row = obj(github.repo_api(f"issues/{number}"))
    return {
        str(label.get("name"))
        for label in row.get("labels") or []  # type: ignore[union-attr]
        if isinstance(label, dict)
    }


def _write(github: GitHub, number: int, add: tuple[str, ...], remove: tuple[str, ...]) -> None:
    if add:
        fields = [arg for label in add for arg in ("-f", f"labels[]={label}")]
        github.gh("api", f"repos/{github.repo}/issues/{number}/labels", "--method", "POST", *fields)
    for label in remove:
        try:
            github.gh(
                "api",
                f"repos/{github.repo}/issues/{number}/labels/{quote(label, safe='')}",
                "--method",
                "DELETE",
            )
        except GitHubNotFoundError:
            pass  # already gone: the change we wanted


def repair(
    github: GitHub,
    *,
    delete_legacy: bool = False,
    now: datetime | None = None,
    snapshot: frontier.Frontier | None = None,
    items: list[dict[str, Any]] | None = None,
) -> Report:
    """Apply each planned change that still matches the item's live labels."""
    report, plans = audit(github, now=now, snapshot=snapshot, items=items)
    report.mode = "repair"
    for row, result in plans:
        number = result.number
        before = {
            str(label.get("name")) for label in row.get("labels") or [] if isinstance(label, dict)
        }
        try:
            live = _fresh_labels(github, number)
        except GitHubError as exc:
            report.skipped.append({"target": f"{result.kind}:{number}", "reason": str(exc)})
            continue
        if live != before:
            # Someone changed the item since the audit read it. The next pass
            # plans from that newer state instead of overwriting it.
            report.skipped.append(
                {"target": f"{result.kind}:{number}", "reason": "labels changed since audit"}
            )
            continue
        try:
            _write(github, number, result.add, result.remove)
        except GitHubError as exc:
            report.skipped.append({"target": f"{result.kind}:{number}", "reason": str(exc)})
            continue
        report.applied.append(result.as_dict(before))
    if delete_legacy:
        for name in report.legacy_definitions:
            try:
                github.gh(
                    "api",
                    f"repos/{github.repo}/labels/{quote(name, safe='')}",
                    "--method",
                    "DELETE",
                )
            except GitHubError as exc:
                report.skipped.append({"target": f"label:{name}", "reason": str(exc)})
                continue
            report.deleted_definitions.append(name)
    return report
