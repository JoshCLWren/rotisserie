from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPT_DIR = Path(__file__).parent
CONTROLLER_PATH = SCRIPT_DIR / "factory-review-controller.py"
VERDICT_PATH = SCRIPT_DIR / "factory-semantic-verdict.sh"
HEAD = "a" * 40
OLD_HEAD = "b" * 40


def load_controller():
    spec = importlib.util.spec_from_file_location("factory_review_controller", CONTROLLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_model_provenance(
    monkeypatch: pytest.MonkeyPatch, controller: ModuleType, head: str
) -> None:
    """Supply trusted independent model identities for mechanical-gate fixtures."""
    markers = [
        controller.agent_model_author_marker(pr=123, head=head, model="model-a", agent="10"),
        controller.agent_model_review_marker(
            pr=123, head=head, author_model="model-a", reviewer_model="model-b",
            author_agent="10", reviewer_agent="11",
        ),
    ]
    monkeypatch.setattr(controller, "model_comment_bodies", lambda _pr: markers)


def test_canonical_factory_branch_honors_matching_part_of_without_changing_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = load_controller()
    current = {"body": "Part of #4740."}
    monkeypatch.setattr(controller, "pr_json", lambda _pr: current)
    branch = "factory/75-4740-opencode-free"
    assert controller.linked_issue_mode_for_pr(4934, branch) == (4740, True)
    current["body"] = "Part of #9999."
    assert controller.linked_issue_mode_for_pr(4934, branch) == (4740, False)
    current["body"] = "Closes #4740.\nPart of #4740."
    assert controller.linked_issue_mode_for_pr(4934, branch) == (4740, False)
    current["body"] = "Part of #4740."
    assert controller.linked_issue_mode_for_pr(4934, "local/review") == (4740, True)
    # handle_review already fetched this PR; avoid another remote read.
    def unexpected_fetch(_pr: int) -> dict[str, object]:
        raise AssertionError("linked issue mode should reuse the PR snapshot")

    monkeypatch.setattr(controller, "pr_json", unexpected_fetch)
    assert controller.linked_issue_mode_for_pr(
        4934, branch, body="Part of #4740."
    ) == (4740, True)


def test_required_checks_no_checks_configured_retries():
    controller = load_controller()
    result = controller.interpret_required_checks(
        [], command_status=1, stderr="no required checks reported for this branch"
    )
    assert result["decision"] == "retry"
    assert "blocked until CI runs" in result["reason"]


def test_required_checks_empty_payload_with_success_status_retries():
    """An empty required-check payload must never pass, whatever gh exit code says."""
    controller = load_controller()
    result = controller.interpret_required_checks([], command_status=0, stderr="")
    assert result["decision"] == "retry"


def test_required_checks_none_with_no_required_checks_stderr_retries():
    controller = load_controller()
    result = controller.interpret_required_checks(
        None, command_status=1, stderr="no required checks reported for this branch"
    )
    assert result["decision"] == "retry"


def test_required_checks_pending_retries():
    controller = load_controller()
    result = controller.interpret_required_checks(
        [{"state": "PENDING"}], command_status=8, stderr=""
    )
    assert result["decision"] == "retry"


def test_required_checks_action_required_retries():
    controller = load_controller()
    result = controller.interpret_required_checks(
        [{"state": "ACTION_REQUIRED"}], command_status=1, stderr=""
    )
    assert result["decision"] == "retry"


def test_required_checks_failure_denies():
    controller = load_controller()
    result = controller.interpret_required_checks(
        [{"state": "FAILURE"}], command_status=1, stderr=""
    )
    assert result["decision"] == "deny"


def test_required_checks_all_passing_passes():
    controller = load_controller()
    result = controller.interpret_required_checks(
        [{"state": "SUCCESS"}, {"state": "SKIPPED"}, {"state": "NEUTRAL"}],
        command_status=0,
        stderr="",
    )
    assert result["decision"] == "pass"


def test_ci_reconciliation_classifies_pending_as_retry():
    controller = load_controller()
    assert (
        controller.classify_ci_reconciliation(checks_decision="retry", authorized=False)
        == "retry-ci"
    )


def test_ci_reconciliation_classifies_failed_checks_as_repair():
    controller = load_controller()
    assert (
        controller.classify_ci_reconciliation(checks_decision="deny", authorized=False)
        == "repair-ci"
    )


def test_ci_reconciliation_promotes_only_authorized_green_exact_head():
    controller = load_controller()
    assert (
        controller.classify_ci_reconciliation(
            checks_decision="pass", authorized=True, mechanical_decision="pass"
        )
        == "ready"
    )
    assert (
        controller.classify_ci_reconciliation(
            checks_decision="pass", authorized=False, mechanical_decision="pass"
        )
        == "review"
    )


def test_ci_reconciliation_rejects_stale_or_missing_authorization():
    controller = load_controller()
    assert (
        controller.classify_ci_reconciliation(checks_decision="pass", authorized=False) == "review"
    )


def test_ci_reconciliation_keeps_repairable_mechanical_failures_executable():
    controller = load_controller()
    assert (
        controller.classify_ci_reconciliation(
            checks_decision="pass", authorized=True, mechanical_decision="deny"
        )
        == "changes-requested"
    )
    assert (
        controller.classify_ci_reconciliation(
            checks_decision="pass", authorized=True, mechanical_decision="retry"
        )
        == "retry-ci"
    )


def test_reconcile_ci_promotes_green_authorized_pr_without_worker(monkeypatch):
    controller = load_controller()
    head = "c" * 40
    monkeypatch.setattr(
        controller,
        "pr_json",
        lambda _pr: {
            "state": "OPEN",
            "headRefOid": head,
            "headRefName": "factory/10-123-fix",
            "body": "Worker: opencode-free-model-factory-10",
            "labels": [
                {"name": "factory"},
                {"name": "factory:unowned"},
                {"name": "factory:ci"},
            ],
        },
    )
    monkeypatch.setattr(
        controller, "required_checks_gate", lambda _pr: {"decision": "pass", "reason": "green"}
    )
    monkeypatch.setattr(
        controller,
        "review_comment_bodies",
        lambda _pr: [
            controller.head_contributor_marker(pr=123, head=head, worker="10", epoch=1),
            controller.review_marker(
                pr=123, head=head, reviewer="11", producer="10", verdict="approve"
            )
        ],
    )
    monkeypatch.setattr(
        controller,
        "mechanical_merge_gate",
        lambda _pr, _head: {"decision": "pass", "reason": "green"},
    )
    monkeypatch.setattr(controller, "model_comment_bodies", lambda _pr: [])
    monkeypatch.setattr(
        controller, "head_has_cross_model_review", lambda *_a, **_kw: True
    )
    writes = []
    monkeypatch.setattr(controller, "replace_factory_labels", lambda *args: writes.append(args))

    result = controller.reconcile_ci_pr(123)

    assert result["status"] == "ready"
    assert writes == [(123, "factory:unowned", "factory:ready")]


def test_reconcile_ci_routes_conflicted_green_pr_to_repair(monkeypatch):
    controller = load_controller()
    head = "d" * 40
    configure_model_provenance(monkeypatch, controller, head)
    monkeypatch.setattr(
        controller,
        "pr_json",
        lambda _pr: {
            "state": "OPEN",
            "headRefOid": head,
            "headRefName": "factory/10-123-fix",
            "body": "Worker: opencode-free-model-factory-10",
            "labels": [{"name": "factory:ci"}, {"name": "factory:unowned"}],
        },
    )
    monkeypatch.setattr(
        controller, "required_checks_gate", lambda _pr: {"decision": "pass", "reason": "green"}
    )
    monkeypatch.setattr(
        controller,
        "review_comment_bodies",
        lambda _pr: [
            controller.head_contributor_marker(pr=123, head=head, worker="10", epoch=1),
            controller.review_marker(
                pr=123, head=head, reviewer="11", producer="10", verdict="approve"
            )
        ],
    )
    monkeypatch.setattr(
        controller,
        "mechanical_merge_gate",
        lambda _pr, _head: {"decision": "deny", "reason": "pull request has merge conflicts"},
    )
    writes = []
    posted = []
    events = []
    monkeypatch.setattr(
        controller,
        "post_review_comment",
        lambda **kwargs: (events.append("comment"), posted.append(kwargs)),
    )
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda *args: (events.append("transition"), writes.append(args)),
    )

    result = controller.reconcile_ci_pr(123)

    assert result["status"] == "changes-requested"
    assert events == ["comment", "transition"]
    assert posted[0]["excerpt"] == "pull request has merge conflicts"
    assert head in posted[0]["note"]
    assert writes == [(123, "factory:unowned", "factory:changes-requested")]


def test_reconcile_ci_does_not_transition_when_findings_persistence_fails(monkeypatch):
    controller = load_controller()
    head = "d" * 40
    configure_model_provenance(monkeypatch, controller, head)
    monkeypatch.setattr(
        controller,
        "pr_json",
        lambda _pr: {
            "state": "OPEN",
            "headRefOid": head,
            "headRefName": "factory/10-123-fix",
            "body": "Worker: opencode-free-model-factory-10",
            "labels": [{"name": "factory:ci"}, {"name": "factory:unowned"}],
        },
    )
    monkeypatch.setattr(
        controller,
        "required_checks_gate",
        lambda _pr: {"decision": "pass", "reason": "green"},
    )
    monkeypatch.setattr(
        controller,
        "review_comment_bodies",
        lambda _pr: [
            controller.head_contributor_marker(pr=123, head=head, worker="10", epoch=1),
            controller.review_marker(
                pr=123, head=head, reviewer="11", producer="10", verdict="approve"
            )
        ],
    )
    monkeypatch.setattr(
        controller,
        "mechanical_merge_gate",
        lambda _pr, _head: {"decision": "deny", "reason": "pull request has merge conflicts"},
    )
    writes = []
    monkeypatch.setattr(controller, "replace_factory_labels", lambda *args: writes.append(args))

    def fail_comment(**_kwargs):
        raise RuntimeError("GitHub comment write failed")

    monkeypatch.setattr(controller, "post_review_comment", fail_comment)

    with pytest.raises(RuntimeError, match="GitHub comment write failed"):
        controller.reconcile_ci_pr(123)

    assert writes == []


def configure_review_handoff(
    monkeypatch, controller, *, verdict="approve", reason="pull request has merge conflicts"
):
    """Configure an independently leased exact-head review with a mechanical denial."""
    monkeypatch.setattr(
        controller,
        "pr_json",
        lambda _pr: {
            "state": "OPEN",
            "headRefOid": HEAD,
            "headRefName": "factory/10-123-fix",
            "body": "Worker: opencode-free-model-factory-10",
            "labels": [{"name": "factory:review"}, {"name": "factory:11"}],
        },
    )
    monkeypatch.setattr(controller, "target_owned_by_worker", lambda _number, _worker: True)
    monkeypatch.setattr(
        controller,
        "review_excerpt",
        lambda _path, worker: (
            "Fix the broken authorization boundary"
            if verdict == "repair"
            else "Inspected with `gh pr diff 123` before FACTORY_GATE_READY"
        ),
    )
    monkeypatch.setattr(controller, "review_comment_bodies", lambda _pr: [])
    configure_model_provenance(monkeypatch, controller, HEAD)
    # These cases exercise mechanical handoff after semantic prerequisites.
    # Delivery/acceptance refusal has focused controller integration coverage.
    monkeypatch.setattr(controller, "target_json", lambda _issue: {
        "state": "open", "body": "Implement the requested behavior",
    })
    monkeypatch.setattr(controller, "implementation_delivery_gate", lambda *_args: {
        "decision": "pass", "reason": "fixture source delivery verified",
    })
    monkeypatch.setattr(controller.issue_acceptance, "validate", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(controller, "run_gh", lambda *_args, **_kwargs: SimpleNamespace(
        stdout="app/feature.py\n", stderr="", returncode=0,
    ))
    monkeypatch.setattr(controller, "attest_exact_head_author", lambda **_kwargs: None)
    monkeypatch.setattr(controller, "persist_cross_model_review_marker", lambda **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "roster_model_for_worker",
        lambda worker: f"roster-model-{worker}",
    )
    monkeypatch.setattr(
        controller,
        "mechanical_merge_gate",
        lambda _pr, _head: {"decision": "deny", "reason": reason},
    )
    return {
        "worker": "11",
        "pr_number": 123,
        "verdict": verdict,
        "reviewed_head": HEAD,
        "review_log": None,
    }


def test_approved_mechanical_failure_persists_findings_before_repair(monkeypatch):
    controller = load_controller()
    arguments = configure_review_handoff(monkeypatch, controller)
    events = []
    comments = []
    transitions = []
    monkeypatch.setattr(
        controller,
        "post_review_comment",
        lambda **kwargs: (events.append(("comment", kwargs["verdict"])), comments.append(kwargs)),
    )
    monkeypatch.setattr(
        controller,
        "transition_pr_and_linked_issue",
        lambda **kwargs: (
            events.append(("transition", kwargs["pr_stage"])),
            transitions.append(kwargs),
        ),
    )

    result = controller.handle_review(**arguments)

    assert result["status"] == "approved-mechanical-failure"
    assert events == [
        ("comment", "approve"),
        ("comment", "repair"),
        ("transition", "factory:changes-requested"),
    ]
    assert comments[1]["excerpt"] == "pull request has merge conflicts"
    assert transitions[0]["pr_stage"] == "factory:changes-requested"


@pytest.mark.parametrize("reason", ["", "   ", "repair required", "FACTORY_GATE_BLOCKED"])
def test_approved_mechanical_failure_rejects_non_actionable_findings(monkeypatch, reason):
    controller = load_controller()
    arguments = configure_review_handoff(monkeypatch, controller, reason=reason)
    comments = []
    transitions = []
    monkeypatch.setattr(controller, "post_review_comment", lambda **kwargs: comments.append(kwargs))
    monkeypatch.setattr(
        controller,
        "transition_pr_and_linked_issue",
        lambda **kwargs: transitions.append(kwargs),
    )

    with pytest.raises(
        RuntimeError, match="repair handoff requires durable actionable review findings"
    ):
        controller.handle_review(**arguments)

    assert [comment["verdict"] for comment in comments] == ["approve"]
    assert transitions == []


def test_approved_mechanical_failure_does_not_transition_when_persistence_fails(monkeypatch):
    controller = load_controller()
    arguments = configure_review_handoff(monkeypatch, controller)
    transitions = []

    def persist_approval_only(**kwargs):
        if kwargs["verdict"] == "repair":
            raise RuntimeError("GitHub comment write failed")

    monkeypatch.setattr(controller, "post_review_comment", persist_approval_only)
    monkeypatch.setattr(
        controller,
        "transition_pr_and_linked_issue",
        lambda **kwargs: transitions.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="GitHub comment write failed"):
        controller.handle_review(**arguments)

    assert transitions == []


def test_semantic_repair_persists_findings_before_repair(monkeypatch):
    controller = load_controller()
    arguments = configure_review_handoff(monkeypatch, controller, verdict="repair")
    events = []
    monkeypatch.setattr(
        controller,
        "post_review_comment",
        lambda **kwargs: events.append(("comment", kwargs["excerpt"])),
    )
    monkeypatch.setattr(
        controller,
        "transition_pr_and_linked_issue",
        lambda **kwargs: events.append(("transition", kwargs["pr_stage"])),
    )

    result = controller.handle_review(**arguments)

    assert result["status"] == "repair"
    assert events == [
        ("comment", "Fix the broken authorization boundary"),
        ("transition", "factory:changes-requested"),
    ]


def test_semantic_repair_rejects_non_actionable_findings(monkeypatch):
    controller = load_controller()
    arguments = configure_review_handoff(monkeypatch, controller, verdict="repair")
    monkeypatch.setattr(controller, "review_excerpt", lambda _path, worker: "FACTORY_GATE_BLOCKED")
    comments = []
    transitions = []
    monkeypatch.setattr(controller, "post_review_comment", lambda **kwargs: comments.append(kwargs))
    monkeypatch.setattr(
        controller,
        "transition_pr_and_linked_issue",
        lambda **kwargs: transitions.append(kwargs),
    )

    result = controller.handle_review(**arguments)

    assert result["status"] == "insufficient-review-findings"
    assert comments[0]["verdict"] == "not-ready"
    assert comments[0]["marker"] is None
    assert transitions[0]["pr_stage"] == "factory:review"


def test_reconcile_ci_returns_changed_head_to_review(monkeypatch):
    """Require fresh authorization when the head changes during reconciliation."""
    controller = load_controller()
    head = "c" * 40
    configure_model_provenance(monkeypatch, controller, head)
    changed_head = "d" * 40
    states = iter(
        [
            {
                "state": "OPEN",
                "headRefOid": head,
                "headRefName": "factory/10-123-fix",
                "body": "Worker: opencode-free-model-factory-10",
                "labels": [{"name": "factory:ci"}, {"name": "factory:unowned"}],
            },
            {"headRefOid": changed_head},
        ]
    )
    monkeypatch.setattr(controller, "pr_json", lambda _pr: next(states))
    monkeypatch.setattr(
        controller,
        "required_checks_gate",
        lambda _pr: {"decision": "pass", "reason": "green"},
    )
    monkeypatch.setattr(
        controller,
        "review_comment_bodies",
        lambda _pr: [
            controller.head_contributor_marker(pr=123, head=head, worker="10", epoch=1),
            controller.review_marker(
                pr=123, head=head, reviewer="11", producer="10", verdict="approve"
            )
        ],
    )
    monkeypatch.setattr(
        controller,
        "mechanical_merge_gate",
        lambda _pr, _head: {"decision": "deny", "reason": "pull request head changed"},
    )
    writes = []
    monkeypatch.setattr(controller, "replace_factory_labels", lambda *args: writes.append(args))

    result = controller.reconcile_ci_pr(123)

    assert result == {"pr": 123, "status": "review", "head": changed_head}
    assert writes == [(123, "factory:unowned", "factory:review")]


def test_reconcile_ci_propagates_github_failures(monkeypatch, capsys):
    """Fail the dispatcher when a PR cannot be read or updated."""
    controller = load_controller()
    monkeypatch.setattr(controller, "list_ci_pr_numbers", lambda: [123])

    def fail_reconciliation(_pr):
        raise RuntimeError("GitHub update failed")

    monkeypatch.setattr(controller, "reconcile_ci_pr", fail_reconciliation)

    with pytest.raises(RuntimeError, match="GitHub update failed"):
        controller.reconcile_ci()

    assert '"status": "retry-ci"' in capsys.readouterr().err


def test_mergeable_unknown_then_mergeable(monkeypatch):
    controller = load_controller()
    states = iter(
        [
            {"state": "OPEN", "isDraft": False, "mergeable": "UNKNOWN", "headRefOid": HEAD},
            {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "headRefOid": HEAD},
        ]
    )
    monkeypatch.setattr(controller, "pr_json", lambda _pr: next(states))
    monkeypatch.setattr(controller.time, "sleep", lambda _seconds: None)

    result = controller.poll_mergeable_gate(123, HEAD, poll_attempts=2, poll_interval=0)
    assert result["decision"] == "pass"


def test_stale_unresolved_thread_on_older_head_does_not_block():
    controller = load_controller()
    nodes = [
        {
            "isResolved": False,
            "comments": {
                "nodes": [{"commit": {"oid": OLD_HEAD}}],
                "pageInfo": {"hasNextPage": False},
            },
        }
    ]
    result = controller.interpret_review_threads(nodes, head=HEAD)
    assert result["decision"] == "pass"


def test_unresolved_thread_on_current_head_denies():
    controller = load_controller()
    nodes = [
        {
            "isResolved": False,
            "comments": {
                "nodes": [{"commit": {"oid": HEAD}}],
                "pageInfo": {"hasNextPage": False},
            },
        }
    ]
    result = controller.interpret_review_threads(nodes, head=HEAD)
    assert result["decision"] == "deny"


def bash_function(log_path: Path, expression: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f"source {VERDICT_PATH!s}; {expression}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_verdict_marker_survives_trailing_output(tmp_path):
    log = tmp_path / "review.log"
    log.write_text(
        "Review complete. No blocking findings.\n"
        "FACTORY_GATE_READY\n"
        "Cost: $0.0042\n"
        "Tokens: 1842 input, 77 output\n"
    )
    result = bash_function(log, f"factory_extract_semantic_verdict {log}")
    assert result.returncode == 0
    assert result.stdout.strip() == "approve"


def test_marker_mentioned_in_prose_is_not_authoritative(tmp_path):
    log = tmp_path / "review.log"
    log.write_text("The protocol says to emit FACTORY_GATE_READY when appropriate.\nCost: $0.01\n")
    result = bash_function(log, f"factory_extract_semantic_verdict {log}")
    assert result.returncode != 0


def test_conflicting_exact_markers_still_detected_with_trailers(tmp_path):
    log = tmp_path / "review.log"
    log.write_text("FACTORY_GATE_READY\nFACTORY_GATE_BLOCKED\nTokens: 42\n")
    result = bash_function(log, f"factory_review_has_conflicting_terminal_markers {log}")
    assert result.returncode == 0


def test_terse_review_can_be_substantive_without_changed_path(tmp_path):
    log = tmp_path / "review.log"
    log.write_text(
        "Reviewed the behavior and exact-head constraints. The implementation preserves the trust boundary "
        "and I found no semantic blocker in the requested change."
    )
    result = bash_function(log, f"factory_review_is_substantive {log}")
    assert result.returncode == 0


def test_workflows_delegate_mechanical_gates_to_controller():
    root = SCRIPT_DIR.parent / "workflows"
    for name in ("factory-ready-merge-drain.yml", "fixed-model-factory-dispatch.yml"):
        text = (root / name).read_text()
        assert "factory-review-controller.py" in text
        assert " gates --pr " in text
        for line_number, line in enumerate(text.splitlines(), 1):
            if "gh pr checks" in line and "--help" not in line:
                raise AssertionError(
                    f"{name} evaluates PR checks inline at line {line_number}; "
                    "gate evaluation must stay in factory-review-controller.py"
                )
        assert "reviewThreads(first:100)" not in text
        assert "--json state,isDraft,mergeable,headRefOid" not in text


def test_diff_inspection_failure_does_not_look_like_approve(monkeypatch):
    """Incident #2309: soft-failed approvals must not render as APPROVE."""
    controller = load_controller()
    arguments = configure_review_handoff(monkeypatch, controller)
    monkeypatch.setattr(controller, "review_excerpt", lambda _path, worker: "Looks fine. FACTORY_GATE_READY")
    comments = []
    transitions = []
    monkeypatch.setattr(controller, "post_review_comment", lambda **kwargs: comments.append(kwargs))
    monkeypatch.setattr(
        controller,
        "transition_pr_and_linked_issue",
        lambda **kwargs: transitions.append(kwargs),
    )

    result = controller.handle_review(**arguments)

    assert result["status"] == "diff-inspection-required"
    assert comments[0]["verdict"] == "not-ready"
    assert comments[0]["marker"] is None
    assert transitions[0]["pr_stage"] == "factory:review"


def test_review_comment_bodies_accept_owner_markers(monkeypatch):
    """Owner-authored exact-head markers must authorize during incident response."""
    controller = load_controller()
    marker = controller.review_marker(
        pr=2269,
        head=HEAD,
        reviewer="66",
        producer="48",
        verdict="approve",
    )
    monkeypatch.setattr(
        controller,
        "gh_json",
        lambda _args: [
            [
                {
                    "user": {"login": "JoshCLWren"},
                    "author_association": "OWNER",
                    "body": marker + "\n\nnote",
                },
                {
                    "user": {"login": "random-user"},
                    "author_association": "NONE",
                    "body": marker + "\n\nspoof",
                },
                {
                    "user": {"login": "github-actions[bot]"},
                    "author_association": "NONE",
                    "body": "bot body",
                },
            ]
        ],
    )
    bodies = controller.review_comment_bodies(2269)
    assert bodies == [marker + "\n\nnote", "bot body"]
    model_bodies = controller.model_comment_bodies(2269)
    assert model_bodies == ["bot body"]


def test_gh_pr_view_counts_as_diff_inspection_evidence(monkeypatch):
    """Honest reviews that use gh pr view can still promote past the gate."""
    controller = load_controller()
    arguments = configure_review_handoff(monkeypatch, controller)
    monkeypatch.setattr(
        controller,
        "review_excerpt",
        lambda _path, worker: "Used `gh pr view 123 --json files` then FACTORY_GATE_READY",
    )
    monkeypatch.setattr(
        controller,
        "mechanical_merge_gate",
        lambda _pr, _head: {"decision": "pass", "reason": "green"},
    )
    monkeypatch.setattr(controller, "approval_can_promote", lambda **_kwargs: True)
    comments = []
    transitions = []
    monkeypatch.setattr(controller, "post_review_comment", lambda **kwargs: comments.append(kwargs))
    monkeypatch.setattr(
        controller,
        "transition_pr_and_linked_issue",
        lambda **kwargs: transitions.append(kwargs),
    )

    result = controller.handle_review(**arguments)

    assert result["status"] == "ready"
    assert comments[0]["verdict"] == "approve"
    assert comments[0]["marker"] is not None
    assert comments[0]["marker"].startswith("<!-- comic-pile-factory-semantic-review-v1:")
    assert transitions[0]["pr_stage"] == "factory:ready"


def test_unified_diff_hunk_counts_as_diff_inspection_evidence(monkeypatch):
    """Excerpt tails that only retain dumped unified diffs must still promote.

    The worker dumps `gh pr diff` after sanitize; when that dump exceeds the
    7000-char excerpt window, only hunks like `diff --git` remain. Matching
    those prevents false diff-inspection-required soft-fails (#2309 follow-up).
    """
    controller = load_controller()
    arguments = configure_review_handoff(monkeypatch, controller)
    monkeypatch.setattr(
        controller,
        "review_excerpt",
        lambda _path, worker: (
            "diff --git a/tests/test_example.py b/tests/test_example.py\n"
            "+++ b/tests/test_example.py\n"
            "+def test_ok() -> None:\n"
            "+    assert True\n"
            "FACTORY_GATE_READY"
        ),
    )
    monkeypatch.setattr(
        controller,
        "mechanical_merge_gate",
        lambda _pr, _head: {"decision": "pass", "reason": "green"},
    )
    monkeypatch.setattr(controller, "approval_can_promote", lambda **_kwargs: True)
    comments = []
    transitions = []
    monkeypatch.setattr(controller, "post_review_comment", lambda **kwargs: comments.append(kwargs))
    monkeypatch.setattr(
        controller,
        "transition_pr_and_linked_issue",
        lambda **kwargs: transitions.append(kwargs),
    )

    result = controller.handle_review(**arguments)

    assert result["status"] == "ready"
    assert comments[0]["verdict"] == "approve"
    assert transitions[0]["pr_stage"] == "factory:ready"


def test_authoritative_diff_is_read_but_never_dumped_into_review_log():
    worker = Path(__file__).with_name("free-model-factory-worker.sh").read_text(encoding="utf-8")
    assert 'if ! gh pr diff "$NUMBER" >/dev/null; then' in worker
    assert 'gh pr diff "$NUMBER" 2>/dev/null | head -n 4000' not in worker
    assert 'printf \'gh pr diff %s\\n\' "$NUMBER"' in worker


def test_thousands_of_appended_diff_lines_cannot_evict_semantic_repair_finding():
    controller = load_controller()
    finding = "Changing the auth guard permits unauthorized requests to bypass token validation."
    diff = "diff --git a/app.py b/app.py\n" + "+    return unsafe_code()\n" * 4000
    log = (
        finding
        + "\nFACTORY_GATE_BLOCKED\n"
        + controller.AUTHORITATIVE_DIFF_EVIDENCE
        + "\ngh pr diff 3200\n"
        + diff
    )
    excerpt = controller.semantic_review_excerpt(log)
    assert finding in excerpt
    assert "gh pr diff 3200" in excerpt
    assert "+    return unsafe_code()" not in excerpt
    assert controller.has_actionable_review_findings(excerpt)


@pytest.mark.parametrize(
    "unusable",
    [
        "FACTORY_GATE_BLOCKED",
        "# comic-pile-factory-authoritative-diff-evidence\ngh pr diff 3200",
        "diff --git a/app.py b/app.py\n+++ b/app.py\n+const result = unsafe();",
        "```python\ndef broken():\n    return unsafe()\n```",
        "semantic blockers remain\nthe pr is returning to repair",
    ],
)
def test_diff_source_and_terminal_markers_are_not_actionable(unusable):
    controller = load_controller()
    assert not controller.has_actionable_review_findings(unusable)


@pytest.mark.parametrize("verdict", ["repair", "reject"])
def test_diff_only_semantic_verdict_returns_to_review(monkeypatch, verdict):
    controller = load_controller()
    arguments = configure_review_handoff(monkeypatch, controller, verdict=verdict)
    monkeypatch.setattr(
        controller,
        "review_excerpt",
        lambda _path, worker: "diff --git a/app.py b/app.py\n+const value = true;\nFACTORY_GATE_BLOCKED",
    )
    comments = []
    transitions = []
    monkeypatch.setattr(controller, "post_review_comment", lambda **kw: comments.append(kw))
    monkeypatch.setattr(
        controller, "transition_pr_and_linked_issue", lambda **kw: transitions.append(kw)
    )

    result = controller.handle_review(**arguments)

    assert result["status"] == "insufficient-review-findings"
    assert comments[0]["verdict"] == "not-ready"
    assert transitions[0]["pr_stage"] == "factory:review"


def test_fixed_model_factory_schedules_are_active():
    root = SCRIPT_DIR.parent / "workflows"
    drain = (root / "factory-ready-merge-drain.yml").read_text()
    dispatcher = (root / "fixed-model-factory-dispatch.yml").read_text()
    assert "    - cron: '2-57/15 * * * *'" in drain
    assert dispatcher.count("    - cron: '") == 1
    assert "    - cron: '7 * * * *'" in dispatcher
    assert "gh workflow run factory-ready-merge-drain.yml" in dispatcher


def test_semantic_repair_heads_count_distinct_heads_once():
    policy = load_controller()._review_policy
    head_a = "a" * 40
    head_b = "b" * 40
    comments = [
        policy.review_marker(pr=42, head=head_a, reviewer="7", producer="8", verdict="repair"),
        policy.review_marker(pr=42, head=head_a, reviewer="9", producer="8", verdict="repair"),
        policy.review_marker(pr=42, head=head_b, reviewer="10", producer="8", verdict="repair"),
        policy.review_marker(pr=42, head="c" * 40, reviewer="11", producer="8", verdict="approve"),
        policy.review_marker(pr=43, head="d" * 40, reviewer="12", producer="8", verdict="repair"),
    ]
    assert policy.semantic_repair_heads(comments, pr=42) == {head_a, head_b}


def test_review_controller_has_three_repairs_then_cancel_circuit_breaker():
    controller = load_controller()
    source = Path(controller.__file__).read_text()
    assert "FACTORY_REPAIR_CYCLE_LIMIT = 3" in source
    assert 'status": "strike-limit-cancelled"' in source
    assert "comic-pile-factory-strike-reset-v1" in source


@pytest.mark.parametrize("resolved, decision", [(True, "pass"), (False, "deny")])
def test_review_thread_query_uses_named_graphql_field(
    monkeypatch: pytest.MonkeyPatch, resolved: bool, decision: str
) -> None:
    """Inspect current-head threads through a valid gh field without bypassing blockers."""
    controller = load_controller()
    calls: list[list[str]] = []

    def github_json(args: list[str]) -> object:
        calls.append(args)
        if args[1] == "graphql":
            query_arg = args[args.index("-f") + 1]
            assert query_arg.startswith("query=query($owner:String!")
            assert "reviewThreads(first:100)" in query_arg
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [{"isResolved": resolved, "comments": {
                    "nodes": [{"commit": {"oid": HEAD}}],
                    "pageInfo": {"hasNextPage": False},
                }}],
                "pageInfo": {"hasNextPage": False},
            }}}}}
        if "/comments?" in args[-1]:
            return [[{"commit_id": HEAD}]]
        return [[]]

    monkeypatch.setattr(controller, "gh_json", github_json)
    result = controller.current_head_review_gate(123, HEAD)
    assert result["decision"] == decision
    assert len(calls) == 3


def test_replace_factory_labels_skips_standalone_manual_only_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review/repair label writes must leave a manual-only PR untouched."""
    controller = load_controller()
    writes: list[list[str]] = []
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda _number: {
            "body": "Hold this PR.\nmanual-only\n",
            "labels": [{"name": "factory"}, {"name": "factory:review"}],
        },
    )

    def fake_run_gh(
        args: list[str], *, input_json: object | None = None, check: bool = True
    ) -> str:
        writes.append(list(args))
        return ""

    monkeypatch.setattr(controller, "run_gh", fake_run_gh)

    controller.replace_factory_labels(4512, "factory:unowned", "factory:changes-requested")
    assert writes == []


def test_replace_factory_labels_writes_ordinary_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary review handoffs still replace factory owner/stage labels."""
    controller = load_controller()
    written_labels: list[str] = []
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda _number: {
            "body": "Ordinary factory work.",
            "labels": [{"name": "bug"}, {"name": "factory:ci"}],
        },
    )

    def fake_run_gh(
        args: list[str], *, input_json: object | None = None, check: bool = True
    ) -> str:
        assert any("issues/12/labels" in part for part in args)
        assert isinstance(input_json, dict)
        labels = input_json["labels"]
        assert isinstance(labels, list)
        written_labels.extend(str(label) for label in labels)
        return ""

    monkeypatch.setattr(controller, "run_gh", fake_run_gh)

    controller.replace_factory_labels(12, "factory:unowned", "factory:review")
    assert set(written_labels) == {
        "bug",
        "factory",
        "factory:review",
        "factory:unowned",
    }
