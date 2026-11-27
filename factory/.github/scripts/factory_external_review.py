"""Record an outside agent's formal GitHub review as trusted factory evidence (#191).

Runs only in trusted Actions, inside the ledger's locked handoff of the
agent's review claim. The merge gate is unchanged: this writes the same
bot-authored approval and reviewer-model records the local review attestation
writes. An approval is final: no acceptance record is required.
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
    exclusion = ctrl._review_exclusion.resolve(
        ctrl.repo_fetch,
        pr=pr_number,
        head=head,
        base_branch=str(pr.get("baseRefName") or ""),
        comments=ctrl.model_comment_bodies(pr_number),
        pull=pr,
    )
    clash = exclusion.matches(model)
    if clash:
        raise ValueError(
            f"model {model} authored the current head ({clash[0]}); it cannot review it"
        )

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
    latest = ctrl.pr_json(pr_number)
    if latest.get("headRefOid") != head:
        raise RuntimeError("the PR changed while the review was checked")
    approval = policy.review_marker(
        pr=pr_number, head=head, reviewer=identity, producer=None, verdict="approve"
    )
    excerpt = ctrl.redact_review_text(body)[:7000]
    comments = [
        approval + f"\nOutside review: {actor} ({model})\n" + excerpt,
        policy.reviewer_model_marker(
            pr=pr_number, head=head, reviewer_model=model, reviewer_agent=identity
        ),
    ]
    for comment in comments:
        ctrl.run_gh(["issue", "comment", str(pr_number), "--repo", ctrl.REPO, "--body", comment])
    ctrl.replace_factory_labels(pr_number, "factory:unowned", "factory:ci")
    return {"pr": pr_number, "head": head, "verdict": "approve", "next": "factory:ci"}
