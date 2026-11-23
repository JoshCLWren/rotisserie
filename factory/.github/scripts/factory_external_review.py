"""Record an outside agent's formal GitHub review as trusted factory evidence (#191).

Runs only in trusted Actions, inside the ledger's locked handoff of the
agent's review claim. The merge gate is unchanged: this writes the same
bot-authored approval, reviewer-model and acceptance records the local review
attestation writes, after the same checks.
"""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any


def load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_controller: ModuleType | None = None


def controller() -> ModuleType:
    global _controller
    if _controller is None:
        _controller = load("external_review_controller", "factory-review-controller.py")
    return _controller


def reviewer_identity(actor: str) -> str:
    token = re.sub(r"[^a-z0-9-]+", "-", actor.lower()).strip("-") or "agent"
    return f"external-{token}"[:89]


def declared_model(body: str, policy: ModuleType) -> str:
    models = policy.model_trailer_models(body)
    if len(models) != 1:
        raise ValueError("the review body needs exactly one 'Model: <your model id>' line")
    return str(next(iter(models)))


def commit_trailer_models(pr: int, ctrl: ModuleType) -> set[str]:
    pages = ctrl.gh_json(
        ["api", "--paginate", "--slurp", f"repos/{ctrl.REPO}/pulls/{pr}/commits?per_page=100"]
    )
    found: set[str] = set()
    for page in pages if isinstance(pages, list) else []:
        for commit in page if isinstance(page, list) else []:
            message = ((commit or {}).get("commit") or {}).get("message")
            found |= ctrl._review_policy.model_trailer_models(message)
    return found


def self_review(model: str, authors: set[str], policy: ModuleType) -> str | None:
    return next(
        (author for author in sorted(authors) if policy.same_actual_model(model, author)), None
    )


def record(
    *,
    pr_number: int,
    head: str,
    actor: str,
    review: Mapping[str, Any],
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate one claimed review and publish its trusted records.

    Raises on any failed check, which rolls back the ledger handoff and keeps
    the agent's claim held.
    """
    env = os.environ if environment is None else environment
    if env.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("outside reviews are recorded only in trusted Actions")
    ctrl = controller()
    policy = ctrl._review_policy
    if str((review.get("user") or {}).get("login") or "") != actor:
        raise ValueError("the review was not written by the claimant")
    if review.get("commit_id") != head:
        raise ValueError("the review is not on the claimed head")
    state = str(review.get("state") or "").upper()
    if state not in {"APPROVED", "CHANGES_REQUESTED"}:
        raise ValueError("submit the review as Approve or Request changes")
    body = str(review.get("body") or "")
    model = declared_model(body, policy)

    pr = ctrl.pr_json(pr_number)
    if pr.get("headRefOid") != head or pr.get("state") != "OPEN" or pr.get("isDraft"):
        raise RuntimeError("the PR moved, closed or became a draft")
    if ctrl.active_factory_owner(ctrl.labels_of(pr)) is not None:
        raise RuntimeError("a factory worker owns the PR")
    trailers = commit_trailer_models(pr_number, ctrl)
    authors = trailers | set(
        policy.known_author_models(ctrl.model_comment_bodies(pr_number), pr=pr_number)
    )
    clash = self_review(model, authors, policy)
    if clash is not None:
        raise ValueError(f"model {model} authored this PR ({clash}); it cannot review it")

    identity = reviewer_identity(actor)
    if state == "CHANGES_REQUESTED":
        ctrl.persist_repair_handoff(
            pr_number=pr_number,
            findings=body,
            reviewer=identity,
            note=f"Outside review by {actor} ({model}) requested changes.",
        )
        return {
            "pr": pr_number,
            "head": head,
            "verdict": "repair",
            "next": "factory:changes-requested",
        }

    provenance = ctrl.reconcile_provenance(pr_number=pr_number, expected_head=head)
    if provenance.get("status") not in {"attested", "already-attested"}:
        raise RuntimeError("exact-head authorship is unavailable")
    issue, non_closing = ctrl.linked_issue_mode_for_pr(pr_number, str(pr.get("headRefName") or ""))
    if issue is None:
        raise ValueError("the PR links no issue whose acceptance the review can attest")
    issue_body = ctrl.target_json(issue)["body"]
    changed = ctrl.run_gh(
        ["pr", "diff", str(pr_number), "--repo", ctrl.REPO, "--name-only"]
    ).stdout.splitlines()
    report = ctrl.issue_acceptance.report_from_text(body)
    errors = ctrl.issue_acceptance.validate(
        report, issue, head, issue_body, changed, non_closing=non_closing
    )
    if errors:
        raise ValueError(
            "the review's FACTORY_ACCEPTANCE_V1 report is invalid: " + "; ".join(errors)
        )
    delivery = ctrl.implementation_delivery_gate(pr_number, head, issue_body)
    if delivery["decision"] != "pass":
        raise RuntimeError("implementation delivery gate: " + delivery["reason"])

    latest = ctrl.pr_json(pr_number)
    if latest.get("headRefOid") != head or ctrl.target_json(issue).get("body") != issue_body:
        raise RuntimeError("the PR or its issue changed while the review was checked")
    approval = policy.review_marker(
        pr=pr_number, head=head, reviewer=identity, producer=None, verdict="approve"
    )
    excerpt = ctrl.redact_review_text(body)[:7000]
    comments = [
        approval + f"\nOutside review: {actor} ({model})\n" + excerpt,
        policy.reviewer_model_marker(
            pr=pr_number, head=head, reviewer_model=model, reviewer_agent=identity
        ),
        ctrl.issue_acceptance.marker(issue, head, issue_body),
    ]
    for comment in comments:
        ctrl.run_gh(["issue", "comment", str(pr_number), "--repo", ctrl.REPO, "--body", comment])
    ctrl.replace_factory_labels(pr_number, "factory:unowned", "factory:ci")
    return {"pr": pr_number, "head": head, "verdict": "approve", "next": "factory:ci"}
