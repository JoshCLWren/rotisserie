"""Unit coverage for the shared factory eligibility semantics.

These tests pin the canonical rules in ``scripts/factory_eligibility.py``
that both work-selection paths (``scripts/next_task.py`` and
``.github/scripts/factory_work_policy.py``) must honor without drift.
"""

import scripts.factory_eligibility as eligibility
import scripts.next_task as next_task


def _policy():
    """Load the fixed-model controller policy module."""
    import importlib.util
    import sys
    from pathlib import Path

    scripts_dir = Path("scripts").resolve()
    controller_dir = Path(".github/scripts").resolve()
    for entry in (str(scripts_dir), str(controller_dir)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    spec = importlib.util.spec_from_file_location(
        "factory_work_policy_under_test",
        controller_dir / "factory_work_policy.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_negated_closure_and_part_of_references_fail_closed() -> None:
    """Shared parser never turns explicit noncompletion into issue completion."""
    partial = "Part of #4719. This PR does not close #4719."
    assert eligibility.explicit_closing_issue(partial) is None
    assert eligibility.nonclosing_issue_numbers(partial) == {4719}
    assert eligibility.explicit_closing_issue("Will not fix #4719.") is None
    assert eligibility.explicit_closing_issue("Closes #4740. Part of #4335.") == 4740
    assert eligibility.explicit_closing_issue("Fixes #7. Resolves #8.") == 7


def test_manual_only_standalone_directive_excludes() -> None:
    """A standalone directive line disallows autonomous execution."""
    assert eligibility.is_manual_only("<!-- factory-execution:manual-only -->\nGate.")
    assert eligibility.is_manual_only("Gate.\n   <!-- factory-execution:manual-only -->   \n")
    assert eligibility.is_manual_only("<!--factory-execution:manual-only-->")


def test_manual_only_standalone_line_excludes() -> None:
    """A standalone ``manual-only`` line is the short-form operator hold."""
    assert eligibility.is_manual_only("manual-only")
    assert eligibility.is_manual_only("Revert auth.\nmanual-only\n")
    assert eligibility.is_manual_only("   manual-only   ")


def test_manual_only_prose_discussion_stays_eligible() -> None:
    """Merely discussing the marker in prose must not trigger exclusion."""
    assert not eligibility.is_manual_only(
        "The manual-only marker is reserved for human gates."
    )
    assert not eligibility.is_manual_only(
        "The issue's <!-- factory-execution:manual-only --> marker reserves this."
    )
    assert not eligibility.is_manual_only(
        "Human review boundary plus <!-- factory-execution:manual-only --> hold."
    )
    assert not eligibility.is_manual_only(None)
    assert not eligibility.is_manual_only("")


def test_manual_only_quoted_in_code_stays_eligible() -> None:
    """Quoting the marker in inline code or fenced blocks never excludes."""
    assert not eligibility.is_manual_only(
        "The `<!-- factory-execution:manual-only -->` marker is human-only."
    )
    assert not eligibility.is_manual_only(
        "```\n<!-- factory-execution:manual-only -->\n```\nOrdinary work."
    )
    assert not eligibility.is_manual_only(
        "```markdown\n<!-- factory-execution:manual-only -->\n```"
    )
    assert not eligibility.is_manual_only("`manual-only`\nOrdinary work.")
    assert not eligibility.is_manual_only("```\nmanual-only\n```\nOrdinary work.")


def test_inline_depends_on_single_and_cluster() -> None:
    """Inline declarations capture the leading reference cluster."""
    assert eligibility.parse_declared_dependencies("Depends on #2126") == {2126}
    assert eligibility.parse_declared_dependencies("depends on #42") == {42}
    assert eligibility.parse_declared_dependencies(
        "Depends on #2126, #2127, and #2128 being merged"
    ) == {2126, 2127, 2128}
    assert eligibility.parse_declared_dependencies("Depends on #2126 + #2127") == {
        2126,
        2127,
    }


def test_inline_depends_on_ignores_casual_mentions() -> None:
    """Casual references without a declaration never block."""
    assert eligibility.parse_declared_dependencies("See also #100 and #200.") == set()
    assert eligibility.parse_declared_dependencies(
        "Depends on #10 and #11 being merged; see also #12 for reads."
    ) == {10, 11}
    # The #2553 shape: historical references with no declaration.
    assert eligibility.parse_declared_dependencies(
        "#2551 is complete. Frozen by #2366. #2363 is closed."
    ) == set()


def test_markdown_wrapped_references_still_declare() -> None:
    """Backticks, bold, and links around a reference never hide it."""
    assert eligibility.parse_declared_dependencies("Depends on `#19` being merged.") == {19}
    assert eligibility.parse_declared_dependencies("Depends on **#19** first.") == {19}
    assert eligibility.parse_declared_dependencies(
        "Depends on [#19](https://example.test/issues/19) first."
    ) == {19}


def test_heading_list_declares_dependencies() -> None:
    """A dependency heading followed by a reference list blocks intake."""
    assert eligibility.parse_declared_dependencies("## Depends on\n\n- #2721") == {2721}
    assert eligibility.parse_declared_dependencies(
        "## Dependencies\n\n- #19\n- #21\n"
    ) == {19, 21}
    assert eligibility.parse_declared_dependencies(
        "Blocked by:\n\n1. #7\n2. #8\n"
    ) == {7, 8}
    assert eligibility.parse_declared_dependencies(
        "## Prerequisites\n\n* #3\n"
    ) == {3}


def test_heading_list_stops_at_prose() -> None:
    """List collection ends where the list ends."""
    assert eligibility.parse_declared_dependencies(
        "## Dependencies\n\n- #19\n\nUnrelated #20 mention."
    ) == {19}


def test_heading_without_list_declares_nothing() -> None:
    """A bare heading with no reference items is not a declaration."""
    assert eligibility.parse_declared_dependencies("## Dependencies\n\nNone.") == set()


def test_owner_helpers() -> None:
    """Ownership helpers agree on active, unowned, and absent leases."""
    assert eligibility.owner_of({"factory:58"}) == "factory:58"
    assert eligibility.owner_of({"factory:local"}) == "factory:local"
    assert eligibility.owner_of({"factory:unowned"}) == "factory:unowned"
    assert eligibility.owner_of(set()) is None
    assert eligibility.has_active_factory_owner({"factory:58", "factory:building"})
    assert eligibility.has_active_factory_owner({"factory:local"})
    assert not eligibility.has_active_factory_owner({"factory:unowned"})
    assert not eligibility.has_active_factory_owner(set())
    assert eligibility.is_factory_unowned({"factory:unowned"})
    assert eligibility.is_factory_unowned(set())
    assert not eligibility.is_factory_unowned({"factory:7"})


def test_comment_trust_is_not_an_eligibility_rule() -> None:
    """Marker provenance stays in the policy module, not the eligibility module."""
    for name in ("machine_marker_is_trusted", "comment_is_trusted"):
        assert not hasattr(eligibility, name), name


def test_terminal_labels_match_controller_exclusions() -> None:
    """Terminal labels mirror every terminal check in issue_is_static_candidate."""
    assert eligibility.TERMINAL_LABELS == frozenset({"ralph-status:done", "factory:ready"})
    ready = _verdict({"factory:ready", "factory:unowned"})
    assert not ready.eligible
    assert any("factory:ready" in reason for reason in ready.reasons)
    done = _verdict({"ralph-status:done"})
    assert not done.eligible
    assert any("ralph-status:done" in reason for reason in done.reasons)


def test_explain_marks_fully_eligible_issue() -> None:
    """An unowned, unblocked, dependency-free issue explains as eligible."""
    verdict = eligibility.explain_issue_eligibility(
        number=2553,
        state="OPEN",
        labels={"bug", "ralph-task", "ralph-status:pending", "factory:unowned"},
        body="Frozen contract. #2551 is complete.",
        open_numbers={2553},
    )

    assert verdict.eligible
    assert verdict.reasons == ("eligible: no static exclusion applies",)


def _verdict(
    labels: set[str],
    body: str = "",
    state: str = "OPEN",
    number: int = 10,
    open_numbers: set[int] | None = None,
    suppressing_issue_numbers: set[int] | None = None,
    non_executable_numbers: set[int] | None = None,
    no_diff_attempts: int = 0,
    no_diff_limit: int = 3,
    require_ralph_labels: bool = False,
) -> eligibility.EligibilityVerdict:
    """Build a static verdict for one fixture with shared defaults."""
    return eligibility.explain_issue_eligibility(
        number=number,
        state=state,
        labels=labels,
        body=body,
        open_numbers={number} if open_numbers is None else open_numbers,
        suppressing_issue_numbers=suppressing_issue_numbers,
        non_executable_numbers=non_executable_numbers,
        no_diff_attempts=no_diff_attempts,
        no_diff_limit=no_diff_limit,
        require_ralph_labels=require_ralph_labels,
    )


def test_explain_names_each_static_exclusion() -> None:
    """Every static exclusion surfaces as a human-readable reason."""
    assert not _verdict({"epic"}).eligible
    manual = _verdict(set(), body="<!-- factory-execution:manual-only -->\nGate.")
    assert not manual.eligible
    assert any("manual-only" in reason for reason in manual.reasons)
    owned = _verdict({"factory:58"})
    assert not owned.eligible
    assert any("factory:58" in reason for reason in owned.reasons)
    assert not _verdict({"ralph-status:blocked"}).eligible
    assert not _verdict({"ralph-status:done"}).eligible
    assert not _verdict(set(), state="CLOSED").eligible
    suppressed = _verdict(set(), suppressing_issue_numbers={10})
    assert not suppressed.eligible
    assert any("canonical PR" in reason for reason in suppressed.reasons)
    exhausted = _verdict(set(), no_diff_attempts=3, no_diff_limit=3)
    assert not exhausted.eligible
    assert any("retry budget" in reason for reason in exhausted.reasons)


def test_explain_dependency_reason_names_open_prerequisites() -> None:
    """Unresolved heading-declared dependencies name their open targets."""
    verdict = eligibility.explain_issue_eligibility(
        number=2722,
        state="OPEN",
        labels={"factory:unowned"},
        body="## Depends on\n\n- #2721\n",
        open_numbers={2721, 2722},
    )

    assert not verdict.eligible
    assert any("#2721" in reason for reason in verdict.reasons)


def test_explain_ralph_labels_are_opt_in() -> None:
    """Ralph-selector metadata gates only Ralph selection, never the queue."""
    labels = {"factory:unowned"}
    assert eligibility.explain_issue_eligibility(
        number=1, state="OPEN", labels=labels, body="", open_numbers={1}
    ).eligible
    ralph = eligibility.explain_issue_eligibility(
        number=1,
        state="OPEN",
        labels=labels,
        body="",
        open_numbers={1},
        require_ralph_labels=True,
    )
    assert not ralph.eligible
    assert any("Ralph-selector metadata" in reason for reason in ralph.reasons)


def test_selectors_agree_with_shared_semantics() -> None:
    """Both selectors delegate to the shared module without drift."""
    policy = _policy()
    assert policy.MANUAL_ONLY_MARKER == eligibility.MANUAL_ONLY_MARKER
    assert set(policy.BLOCKED_LABELS) == set(eligibility.BLOCKED_LABELS)
    assert policy.OWNER_RE.pattern == eligibility.FACTORY_OWNER_RE.pattern
    assert policy.owner_of({"factory:58"}) == eligibility.owner_of({"factory:58"})
    assert policy.item_is_unowned({"factory:unowned"}) == eligibility.is_factory_unowned(
        {"factory:unowned"}
    )
    bodies = [
        "<!-- factory-execution:manual-only -->\nGate.",
        "The `<!-- factory-execution:manual-only -->` marker is discussed.",
        "Plain prose about manual-only gates.",
        "Revert auth.\nmanual-only\n",
        "`manual-only`\nOrdinary work.",
        "Depends on #10 and #11.",
        "## Dependencies\n\n- #19\n",
    ]
    for body in bodies:
        assert policy.is_manual_only(body) == eligibility.is_manual_only(body)
        assert policy.is_acceptance_parent(body) == eligibility.is_acceptance_parent(body)
        assert policy.parse_declared_dependencies(body) == (
            eligibility.parse_declared_dependencies(body)
        )
        assert next_task._is_manual_only({"body": body, "number": 1, "title": "t", "labels": [], "url": ""}) == eligibility.is_manual_only(body)
        assert next_task._dependency_numbers(body) == (
            eligibility.parse_declared_dependencies(body)
        )


def _controller():
    """Load the fixed-model work controller module, reusing a cached instance."""
    import importlib.util
    import sys
    from pathlib import Path

    name = "factory_work_controller_under_test"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    scripts_dir = Path("scripts").resolve()
    controller_dir = Path(".github/scripts").resolve()
    for entry in (str(scripts_dir), str(controller_dir)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    spec = importlib.util.spec_from_file_location(
        name,
        controller_dir / "factory-work-controller.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _legacy_intake(monkeypatch) -> None:
    """Neutralize Rotisserie intake so shared rules are tested in isolation."""
    controller = _controller()
    monkeypatch.setattr(controller, "rotisserie_issue_intake", lambda issues, prs: None)


def _issue(number: int, *labels: str, body: str = "", state: str = "OPEN") -> dict:
    """Build a minimal issue payload for controller diagnostics."""
    return {
        "number": number,
        "state": state,
        "title": f"Issue {number}",
        "body": body,
        "labels": [{"name": label} for label in labels],
        "createdAt": "2026-08-16T00:00:00Z",
    }


def _pr(number: int, *, issue: int) -> dict:
    """Build a minimal open-PR payload that canonically owns an issue."""
    return {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "title": f"PR {number}",
        "body": f"Closes #{issue}",
        "headRefName": f"factory/58-{issue}-work",
        "headRefOid": "a" * 40,
        "labels": [],
        "createdAt": "2026-08-16T00:00:00Z",
        "updatedAt": "2026-08-16T00:00:00Z",
    }


def test_controller_explain_reports_static_exclusion_with_gates(monkeypatch) -> None:
    """`explain` names every static exclusion and the dynamic gate state."""
    _legacy_intake(monkeypatch)
    controller = _controller()
    issues = [_issue(10, "factory:unowned"), _issue(11, "factory:unowned", "epic")]
    report = controller.explain_issue(
        11,
        issues=issues,
        prs=[],
        no_diff_attempts_by_issue={},
        include_dynamic_gates=False,
    )

    assert report["eligible"] is False
    assert any("epic/prd" in reason for reason in report["reasons"])
    assert "non-executable" not in " ".join(report["reasons"])
    gates = report["dynamic_gates"]
    assert gates["factory_pr_wip_limit"] == controller.FACTORY_PR_WIP_LIMIT
    assert gates["review_backlog_limit"] == controller.FACTORY_REVIEW_BACKLOG_LIMIT
    assert gates["rotisserie_intake"] == "legacy-unrestricted"
    assert "open_dependency_blocker" not in gates


def test_controller_explain_names_the_canonical_owning_pr(monkeypatch) -> None:
    """`explain` reports the open canonical PR that suppresses an issue."""
    _legacy_intake(monkeypatch)
    controller = _controller()
    issues = [_issue(20, "factory:unowned")]
    report = controller.explain_issue(
        20,
        issues=issues,
        prs=[_pr(900, issue=20)],
        no_diff_attempts_by_issue={},
        include_dynamic_gates=False,
    )

    assert report["eligible"] is False
    assert report["open_canonical_prs"] == [900]
    assert any("canonical PR" in reason for reason in report["reasons"])


def test_controller_explain_renders_operator_text(monkeypatch) -> None:
    """The text renderer names the verdict, reasons, PRs, and every gate."""
    _legacy_intake(monkeypatch)
    controller = _controller()
    report = controller.explain_issue(
        20,
        issues=[_issue(20, "factory:unowned")],
        prs=[_pr(900, issue=20)],
        no_diff_attempts_by_issue={},
        include_dynamic_gates=False,
    )
    rendered = controller.render_explain_report(report)

    assert rendered.startswith("#20: not eligible")
    assert "canonical PR" in rendered
    assert "#900" in rendered
    assert "gate factory_pr_wip_limit:" in rendered


def test_stale_claim_history_cannot_starve_an_unowned_issue(monkeypatch) -> None:
    """Many historical claim/release markers never block an unowned issue."""
    _legacy_intake(monkeypatch)
    controller = _controller()
    history = "\n".join(
        "<!-- comic-pile-factory-implement-claim-v3:issue-2553:opencode-free-model-factory-58:"
        "1750000000:attempt-1 -->"
        for _ in range(5)
    ) + (
        "\n<!-- comic-pile-factory-claim-released-v3:issue-2553:"
        "opencode-free-model-factory-58:1750000000:controller-release -->\n"
    )
    issues = [
        _issue(2553, "factory", "factory:unowned", body=f"Frozen contract.\n{history}"),
        _issue(2554, "factory:unowned"),
    ]
    report = controller.explain_issue(
        2553,
        issues=issues,
        prs=[],
        no_diff_attempts_by_issue={},
        include_dynamic_gates=False,
    )

    assert report["eligible"] is True
    assert list(report["reasons"]) == ["eligible: no static exclusion applies"]
    assert report["open_canonical_prs"] == []


def test_active_lease_blocks_double_claim(monkeypatch) -> None:
    """An active lease owner is reported as blocking, never as assignable."""
    _legacy_intake(monkeypatch)
    controller = _controller()
    issues = [_issue(30, "factory", "factory:building", "factory:58")]
    report = controller.explain_issue(
        30,
        issues=issues,
        prs=[],
        no_diff_attempts_by_issue={},
        include_dynamic_gates=False,
    )

    assert report["eligible"] is False
    assert any("factory:58" in reason for reason in report["reasons"])


def test_next_task_explain_requires_ralph_metadata() -> None:
    """The Ralph-queue report requires Ralph metadata and names what it skips."""
    report = next_task.explain_issue(
        {
            "number": 2553,
            "title": "t",
            "body": "Depends on #2721",
            "labels": [{"name": "factory:unowned"}],
            "url": "u",
        },
        open_numbers={2553, 2721},
    )

    assert report["eligible"] is False
    assert any("ralph-task" in reason for reason in report["reasons"])
    assert any("#2721" in reason for reason in report["reasons"])
    assert report["not_evaluated"]


def test_next_task_explain_reports_an_eligible_issue() -> None:
    """A pending, unowned, dependency-free issue explains as Ralph-eligible."""
    report = next_task.explain_issue(
        {
            "number": 3071,
            "title": "t",
            "body": "Ordinary work.",
            "labels": [
                {"name": "ralph-task"},
                {"name": "ralph-status:pending"},
                {"name": "factory:unowned"},
            ],
            "url": "u",
        },
        open_numbers={3071},
    )

    assert report["eligible"] is True
    assert report["selector"] == "next_task"
    # Ralph eligibility never implies approval for fixed-model dispatch.
    assert report["not_evaluated"] == [
        "canonical open-PR suppression (controller-only gate)",
        "Rotisserie issue-intake authorization (fixed-model only)",
        "rolling no-diff retry budget (fixed-model only)",
        "worker WIP, review-backlog, quota, and capacity (fixed-model dispatch gates)",
        "live GitHub dependency blockers (controller-only gate)",
        "lease liveness and worker-specific strike-retry exclusion (assignment-time gates)",
    ]
