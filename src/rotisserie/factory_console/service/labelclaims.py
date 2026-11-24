"""GitHub label claims: add ``factory:claimed`` to request work (issue #191).

A label is a request, never ownership. The trusted event handler checks the
labeler's write access, the live frontier and the Neon fenced lease through
the same external claim code the workflow bridge uses, then answers on the
item with labels and one status comment edited in place. Removing the label
is the claimant's "finished" signal; evidence decides between hand-off and
release. Every write uses the Actions token, which starts no workflows, so the
handler never re-triggers itself.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
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
from rotisserie.factory_console.service import external, labels

STATUS_MARKER = "<!-- rotisserie-label-claim-status-v1 -->"
BOT = "github-actions[bot]"
DISPATCH = "fixed-model-factory-dispatch.yml"


def _lp() -> ModuleType:
    return runtime.module("factory_label_policy")


def _target(payload: Mapping[str, Any]) -> tuple[str, int]:
    if isinstance(payload.get("pull_request"), dict):
        return "pr", int(payload["pull_request"]["number"])
    issue = obj(payload.get("issue"))
    number = int(str(issue.get("number")))
    return ("pr" if "pull_request" in issue else "issue"), number


def _sender(payload: Mapping[str, Any]) -> tuple[str, bool]:
    sender = obj(payload.get("sender"))
    login = string(sender, "login")
    return login, string(sender, "type") == "Bot" or login.endswith("[bot]")


def _add(github: GitHub, number: int, *names: str) -> None:
    if names:
        fields = [arg for name in names for arg in ("-f", f"labels[]={name}")]
        github.gh("api", f"repos/{github.repo}/issues/{number}/labels", "--method", "POST", *fields)


def _remove(github: GitHub, number: int, *names: str) -> None:
    for name in names:
        try:
            github.gh(
                "api",
                f"repos/{github.repo}/issues/{number}/labels/{quote(name, safe='')}",
                "--method",
                "DELETE",
            )
        except GitHubNotFoundError:
            pass  # already gone: the change we wanted


def status_comment(github: GitHub, number: int, lines: list[str]) -> None:
    """Create or edit the one bot status comment on the item."""
    body = "\n".join([STATUS_MARKER, "**Rotisserie claim status**", "", *lines])
    pages = json.loads(
        github.gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{github.repo}/issues/{number}/comments?per_page=100",
        )
    )
    existing = next(
        (
            row
            for page in (pages if isinstance(pages, list) else [])
            if isinstance(page, list)
            for row in page
            if isinstance(row, dict)
            and string(obj(row.get("user")), "login") == BOT
            and string(row, "body").startswith(STATUS_MARKER)
        ),
        None,
    )
    if existing is None:
        github.gh(
            "api",
            f"repos/{github.repo}/issues/{number}/comments",
            "--method",
            "POST",
            "-f",
            f"body={body}",
        )
    else:
        github.gh(
            "api",
            f"repos/{github.repo}/issues/comments/{existing['id']}",
            "--method",
            "PATCH",
            "-f",
            f"body={body}",
        )


def _holder(github: GitHub, number: int) -> dict[str, object] | None:
    return external.holder(github, number)


def _wake(github: GitHub, kind: str, number: int) -> None:
    """Label writes by the Actions token start no workflow, so ask the dispatcher."""
    try:
        github.workflow_run(DISPATCH, {"target": f"{kind}:{number}", "action": "advance"})
    except GitHubError:
        pass


def request(
    github: GitHub, kind: str, number: int, environment: dict[str, str]
) -> dict[str, object]:
    """Answer one claim request: exactly one Neon lease wins, the label never decides."""
    lp = _lp()
    target = f"{kind}:{number}"
    if string(obj(github.repo_api(f"issues/{number}")), "state") != "open":
        # Closed or merged work needs no answer on the item; the doctor never
        # reads closed items, so a rejection label there would never clear.
        _remove(github, number, lp.CLAIMED)
        status_comment(
            github,
            number,
            [
                f"Not claimed: {target} is closed.",
                f"Reopen {target}, then add `factory:claimed` again.",
            ],
        )
        return {"event": "claim", "target": target, "status": "ignored", "reason": "closed"}
    try:
        outcome = external.reserve_target(
            github, client=lp.LABEL_CLIENT, environment=environment, target=target
        )
    except (RuntimeError, ValueError) as exc:
        outcome = {"status": "conflict", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never echo claim store details
        outcome = {"status": "held", "reason": f"claim store unavailable ({type(exc).__name__})"}
    actor = environment.get("GITHUB_ACTOR", "")
    if outcome.get("status") == "reserved":
        _remove(github, number, lp.AVAILABLE, lp.REJECTED)
        # The owner picked this work, so the factory puts it first from here on.
        for item in sorted({number, *_covered(outcome)}):
            _add(github, item, lp.OVERRIDE)
        handoff = obj(outcome.get("handoff"))  # type: ignore[arg-type]
        status_comment(
            github,
            number,
            [
                f"Accepted: @{actor} holds `{handoff.get('action', '')}` on this item.",
                f"Claim: `{outcome.get('claim_id')}`, expires {outcome.get('expires_at')}.",
                "Pushes and comments by the claimant renew it.",
                f"Done means: {handoff.get('evidence_required', '')}.",
                "Then remove `factory:claimed`. The factory takes the next step.",
            ],
        )
        return {"event": "claim", "target": target, **outcome}
    try:
        current = _holder(github, number)
    except Exception:  # noqa: BLE001 - unknown store: answer the request as rejected
        current = None
    if current and current.get("actor") == actor and current.get("client") == lp.LABEL_CLIENT:
        return {"event": "claim", "target": target, "status": "already-held", **current}
    lines = [
        f"Not claimed for @{actor}: {outcome.get('reason', 'unknown')}.",
        f"Next: {outcome.get('next') or 'add `factory:claimed` again to retry.'}",
    ]
    if current and current.get("client") == lp.LABEL_CLIENT:
        # Another agent's label claim won the race; its label stays.
        lines.insert(0, f"Held by @{current.get('actor')}, claim `{current.get('claim_id')}`.")
    else:
        _remove(github, number, lp.CLAIMED)
    _add(github, number, lp.REJECTED)
    status_comment(github, number, lines)
    return {"event": "claim", "target": target, **outcome}


def _covered(claim: Mapping[str, object]) -> list[int]:
    targets = claim.get("targets")
    return [int(n) for n in targets if isinstance(n, int)] if isinstance(targets, list) else []


def _held_by(github: GitHub, number: int, actor: str) -> dict[str, object] | None:
    try:
        found = _holder(github, number)
    except Exception:  # noqa: BLE001 - unknown store: not proof of a claim
        return None
    if found and found.get("actor") == actor and found.get("client") == _lp().LABEL_CLIENT:
        return found
    return None


def _evidence(github: GitHub, claim: Mapping[str, object]) -> str:
    """The URL that proves the claimed work exists, or "" for a plain release."""
    action = str(claim.get("action"))
    kind, number_text = str(claim.get("target")).split(":")
    if action == "implement":
        core = runtime.module("factory_work_policy")
        rows = json.loads(
            github.gh(
                "pr",
                "list",
                "--repo",
                github.repo,
                "--state",
                "open",
                "--author",
                str(claim.get("actor")),
                "--json",
                "number,isDraft,body,title,headRefName",
            )
        )
        for row in rows if isinstance(rows, list) else []:
            if (
                isinstance(row, dict)
                and not row.get("isDraft")
                and core.issue_explicitly_closed_by_pr(row) == int(number_text)
            ):
                return f"https://github.com/{github.repo}/pull/{row['number']}"
        return ""
    pr = obj(github.repo_api(f"pulls/{number_text}"))
    if action == "review":
        return f"https://github.com/{github.repo}/pull/{number_text}"
    head = string(obj(pr.get("head")), "sha")
    if head and head != claim.get("head"):
        return f"https://github.com/{github.repo}/pull/{number_text}"
    return ""


def finish(
    github: GitHub, kind: str, number: int, environment: dict[str, str]
) -> dict[str, object]:
    """The claimant removed ``factory:claimed``: hand off on evidence, else release."""
    lp = _lp()
    actor = environment.get("GITHUB_ACTOR", "")
    try:
        claim = _holder(github, number)
    except Exception as exc:  # noqa: BLE001
        return {"event": "finish", "status": "held", "reason": type(exc).__name__}
    if not claim or claim.get("client") != lp.LABEL_CLIENT:
        return {"event": "finish", "status": "no-label-claim"}
    if claim.get("actor") != actor:
        # Labels cannot end someone else's lease: put the projection back.
        _add(github, number, lp.CLAIMED)
        status_comment(
            github,
            number,
            [
                f"@{actor} removed `factory:claimed`, but @{claim.get('actor')} holds the claim.",
                "Only the claimant or the operator (`rotisserie factory revoke`) can end it.",
            ],
        )
        return {"event": "finish", "status": "restored", "holder": claim.get("actor")}
    evidence = _evidence(github, claim)
    operation = "handoff" if evidence else "release"
    try:
        result = external.claim_action(
            github,
            client=lp.LABEL_CLIENT,
            claim_id=str(claim["claim_id"]),
            operation=operation,
            environment=environment,
            evidence=evidence,
        )
    except (RuntimeError, ValueError) as exc:
        result = {"status": "held", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the ledger rolled back; the claim stays held
        result = {"status": "held", "reason": f"hand-off failed ({type(exc).__name__})"}
    if result.get("status") != "released":
        _add(github, number, lp.CLAIMED)
        status_comment(
            github,
            number,
            [
                f"@{actor}'s claim is still held: {result.get('reason', 'unknown')}.",
                "Fix that, then remove `factory:claimed` again.",
            ],
        )
        return {"event": "finish", "operation": operation, **result}
    # The lease covered the PR and its issue; the doctor projected the label on both.
    covered = {number, *_covered(claim)}
    for item in sorted(covered):
        _remove(github, item, lp.CLAIMED)
    lines = [f"@{actor} finished `{claim.get('action')}`."]
    if operation == "handoff":
        lines.append(f"Handed off {evidence}. The factory takes the next step.")
        evidence_number = int(evidence.rstrip("/").split("/")[-1])
        _add(github, evidence_number, lp.OVERRIDE)
        _wake(github, "pr", evidence_number)
    else:
        lines.append("No evidence was found, so the claim was released unchanged.")
        _wake(github, kind, number)
    status_comment(github, number, lines)
    return {"event": "finish", "operation": operation, "evidence": evidence, **result}


def renew(github: GitHub, number: int, environment: dict[str, str]) -> dict[str, object]:
    actor = environment.get("GITHUB_ACTOR", "")
    claim = _held_by(github, number, actor)
    if not claim:
        return {"event": "renew", "status": "not-claimant"}
    try:
        result = external.claim_action(
            github,
            client=_lp().LABEL_CLIENT,
            claim_id=str(claim["claim_id"]),
            operation="renew",
            environment=environment,
        )
    except (RuntimeError, ValueError) as exc:
        result = {"status": "held", "reason": str(exc)}
    return {"event": "renew", **result}


def _linked_issue_claim(github: GitHub, pr: Mapping[str, Any], actor: str) -> int | None:
    core = runtime.module("factory_work_policy")
    linked = core.issue_explicitly_closed_by_pr(
        {
            "number": pr.get("number"),
            "body": pr.get("body") or "",
            "title": pr.get("title") or "",
            "headRefName": string(obj(pr.get("head")), "ref"),
        }
    )
    if linked is None:
        return None
    claim = _held_by(github, int(linked), actor)
    return int(linked) if claim and claim.get("action") == "implement" else None


def handle(
    github: GitHub, event: str, payload: Mapping[str, Any], environment: dict[str, str]
) -> dict[str, object]:
    """Route one GitHub event. Unrelated events return ``ignored`` and write nothing."""
    lp = _lp()
    actor, bot = _sender(payload)
    if bot or not actor:
        return {"status": "ignored", "reason": "bot or unknown sender"}
    if environment.get("GITHUB_ACTOR") != actor:
        return {"status": "ignored", "reason": "event sender is not the workflow actor"}
    action = str(payload.get("action") or "")
    label = string(obj(payload.get("label")), "name")
    kind, number = _target(payload)
    if event in ("issues", "pull_request_target") and label == lp.CLAIMED:
        if action == "labeled":
            return request(github, kind, number, environment)
        if action == "unlabeled":
            return finish(github, kind, number, environment)
    if event == "pull_request_target" and action in ("opened", "ready_for_review", "edited"):
        pr = obj(payload.get("pull_request"))
        if pr.get("draft") is True:
            return {"status": "ignored", "reason": "draft pull request"}
        issue = _linked_issue_claim(github, pr, actor)
        if issue is not None:
            return finish(github, "issue", issue, environment)
        return {"status": "ignored", "reason": "no implement claim by this author"}
    if event == "pull_request_target" and action == "synchronize":
        return renew(github, number, environment)
    if event == "issue_comment" and action == "created":
        return renew(github, number, environment)
    return {"status": "ignored", "reason": f"{event}.{action} is not a claim event"}


def doctor_after(github: GitHub, number: int) -> dict[str, object]:
    """Repair the one item the event touched, so its projection settles at once."""
    row = obj(github.repo_api(f"issues/{number}"))
    return labels.repair(github, items=[row]).to_dict()
