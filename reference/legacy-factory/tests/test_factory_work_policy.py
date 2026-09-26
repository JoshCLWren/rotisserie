"""Regression coverage for factory assignment review capacity and independence."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "factory_work_policy",
    SCRIPTS / "factory_work_policy.py",
)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)

Candidate = policy.Candidate


def candidate(
    *,
    kind: str,
    number: int,
    stage: str | None = None,
    producer: str | None = None,
    lane: int = 3,
) -> Candidate:
    """Build a deterministic candidate fixture."""
    return Candidate(
        kind=kind,
        number=number,
        lane=lane,
        priority=0,
        created_at="2026-08-16T00:00:00Z",
        stage=stage,
        producer_worker=producer,
    )


def issue_fixture(number: int) -> dict[str, object]:
    """Build an unowned issue that would otherwise be executable."""
    return {
        "number": number,
        "state": "OPEN",
        "title": f"Issue {number}",
        "labels": [{"name": "factory:unowned"}],
        "createdAt": "2026-08-16T00:00:00Z",
    }


def pr_fixture(
    *,
    number: int,
    issue: int,
    labels: list[str],
    state: str = "OPEN",
    draft: bool = False,
    head: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    mergeable: str = "MERGEABLE",
    merge_state: str = "CLEAN",
) -> dict[str, object]:
    """Build a canonical fixed-model PR linked to one issue."""
    return {
        "number": number,
        "state": state,
        "isDraft": draft,
        "labels": [{"name": label} for label in labels],
        "headRefName": f"factory/18-{issue}-nvidia",
        "headRefOid": head,
        "body": "Worker: opencode-free-model-factory-18",
        "createdAt": "2026-08-16T01:00:00Z",
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
    }


def label_names(item: dict[str, object]) -> set[str]:
    """Return label names from a GitHub issue/PR fixture."""
    raw = item["labels"]
    assert isinstance(raw, list)
    names: set[str] = set()
    for label in raw:
        assert isinstance(label, dict)
        name = label["name"]
        assert isinstance(name, str)
        names.add(name)
    return names


def test_producing_worker_cannot_receive_own_semantic_review():
    """Review assignment itself enforces producer/reviewer independence."""
    own_review = candidate(
        kind="pr",
        number=1390,
        stage="factory:review",
        producer="43",
    )
    issue = candidate(kind="issue", number=1500)
    ordered = policy.order_candidates_for_worker([own_review, issue], "43")
    assert own_review not in ordered
    assert ordered == [issue]


def test_producer_can_receive_repair_work_without_self_approving():
    """Independence applies to semantic approval, not ordinary repair."""
    repair = candidate(
        kind="pr",
        number=1390,
        stage="factory:changes-requested",
        producer="43",
    )
    ordered = policy.order_candidates_for_worker([repair], "43")
    assert ordered == [repair]


def test_reserved_review_worker_prefers_factory_review():
    """A stable minority of the fleet gives review dependable capacity."""
    assert policy.review_capacity_worker("6")
    review = candidate(kind="pr", number=1390, stage="factory:review", producer="43")
    issue = candidate(kind="issue", number=1500, lane=1)
    ordered = policy.order_candidates_for_worker([issue, review], "6")
    assert ordered[0] == review


def test_non_reserved_worker_preserves_product_capacity():
    """Ordinary workers keep product implementation moving alongside review."""
    # Worker 9 is outside the idle (~25%) review-first cohort under the ratio.
    assert not policy.review_capacity_worker("9")
    review = candidate(kind="pr", number=1390, stage="factory:review", producer="43")
    issue = candidate(kind="issue", number=1500, lane=5)
    ordered = policy.order_candidates_for_worker([review, issue], "9")
    assert ordered[0] == issue


def test_review_share_scales_continuously_with_backlog():
    """Completion pressure uses a ratio, not absolute magic tiers."""
    assert policy.review_share_for_backlog(0) == 0.25
    assert policy.review_share_for_backlog(10) == 0.25 + (0.65 * 10 / 20.0)
    assert policy.review_share_for_backlog(20) == 0.90
    assert policy.review_share_for_backlog(80) == 0.90
    idle = sum(1 for worker in range(6, 76) if policy.review_capacity_worker(str(worker), review_backlog=0))
    hot = sum(1 for worker in range(6, 76) if policy.review_capacity_worker(str(worker), review_backlog=20))
    assert 15 <= idle <= 25
    assert 55 <= hot <= 70


def test_stage_precedence_is_deterministic_for_inconsistent_labels():
    """Transient contradictory labels never randomize semantic review classification."""
    labels = {"factory:ci", "factory:review", "factory:building"}
    assert policy.stage_of(labels) == "factory:review"
    assert policy.stage_of(labels) == "factory:review"


def test_shared_producer_provenance_drives_assignment():
    """Assignment derives producer identity through the canonical review policy."""
    pr = {
        "headRefName": "factory/43-1386-opencode-free",
        "body": "Worker: opencode-free-model-factory-17",
    }
    assert policy.producer_worker_from_pr(pr) == "43"


def test_closed_pr_is_never_a_candidate():
    """Autonomous assignment never resurrects a closed PR."""
    pr = {
        "number": 1390,
        "state": "CLOSED",
        "isDraft": False,
        "labels": [
            {"name": "factory"},
            {"name": "factory:unowned"},
            {"name": "factory:review"},
        ],
        "headRefName": "factory/43-1386-opencode-free",
        "body": "Worker: opencode-free-model-factory-43",
        "createdAt": "2026-08-16T00:00:00Z",
    }
    assert not policy.pr_is_static_candidate(pr, {})


def test_open_owned_pr_suppresses_fresh_issue_implementation():
    """An in-flight review lease cannot make its issue reappear as new work."""
    issue = issue_fixture(1487)
    pr = pr_fixture(
        number=1512,
        issue=1487,
        labels=["factory", "factory:18", "factory:review"],
    )

    candidates = policy.build_candidates([issue], [pr])

    assert all(candidate.number != 1487 or candidate.kind != "issue" for candidate in candidates)
    assert candidates == []


def test_open_blocked_or_draft_pr_still_suppresses_duplicate_issue_work():
    """Temporarily ineligible PR states fail closed instead of spawning replacements."""
    issue = issue_fixture(1399)
    blocked = pr_fixture(
        number=1510,
        issue=1399,
        labels=["factory", "factory:unowned", "factory:blocked"],
    )
    draft = pr_fixture(
        number=1510,
        issue=1399,
        labels=["factory", "factory:unowned", "factory:review"],
        draft=True,
    )

    for pr in (blocked, draft):
        candidates = policy.build_candidates([issue], [pr])
        assert all(
            candidate.number != 1399 or candidate.kind != "issue"
            for candidate in candidates
        )


def test_urgent_bug_with_open_blocked_pr_still_has_one_canonical_pr():
    """Urgency never permits a second implementation PR for the same issue."""
    issue = issue_fixture(1882)
    issue["labels"] = [
        {"name": "factory:unowned"},
        {"name": "user-reported"},
        {"name": "bug"},
    ]
    blocked = pr_fixture(
        number=1897,
        issue=1882,
        labels=["factory", "factory:unowned", "factory:blocked"],
    )

    candidates = policy.build_candidates([issue], [blocked])

    assert all(
        candidate.number != 1882 or candidate.kind != "issue"
        for candidate in candidates
    )


def test_closed_pr_releases_issue_back_to_implementation_queue():
    """Closing the canonical PR makes its still-open issue eligible again."""
    issue = issue_fixture(1487)
    closed = pr_fixture(
        number=1512,
        issue=1487,
        labels=["factory", "factory:unowned", "factory:review"],
        state="CLOSED",
    )

    candidates = policy.build_candidates([issue], [closed])

    assert [(candidate.kind, candidate.number) for candidate in candidates] == [
        ("issue", 1487)
    ]


def test_plan_distinct_assignments_reserves_review_without_stopping_product():
    """One batch can assign review and implementation concurrently."""
    review = candidate(kind="pr", number=1390, stage="factory:review", producer="43")
    issue_a = candidate(kind="issue", number=1500)
    issue_b = candidate(kind="issue", number=1501)
    assignments = policy.plan_distinct_assignments(
        [review, issue_a, issue_b],
        ["6", "9"],
    )
    assert assignments["6"] == review
    assert assignments["9"].kind == "issue"


# ── parse_depends_on_numbers and body_depends_on_unresolved unit tests ──


def test_parse_depends_on_single_prerequisite():
    """Single 'Depends on #NNN' returns one number."""
    assert policy.parse_depends_on_numbers("Depends on #2126") == {2126}


def test_parse_depends_on_multiple_prerequisites():
    """Comma-separated prerequisites are all extracted."""
    body = "Depends on #2126, #2127, and #2128 being merged"
    assert policy.parse_depends_on_numbers(body) == {2126, 2127, 2128}


def test_parse_depends_on_case_insensitive():
    """Both 'Depends on' and 'depends on' are recognized."""
    assert policy.parse_depends_on_numbers("depends on #42") == {42}


def test_parse_depends_on_casual_mentions_not_matched():
    """Casual '#N' mentions without 'Depends on' prefix are ignored."""
    body = "See also #100 and #200 for context."
    assert policy.parse_depends_on_numbers(body) == set()


def test_parse_depends_on_later_lines_do_not_block():
    """'#N' mentions on lines after a declaration are not prerequisites."""
    body = "Depends on #2126.\n\nRelated cleanup tracked in #2104 for context."
    assert policy.parse_depends_on_numbers(body) == {2126}


def test_parse_depends_on_plus_separated_references():
    """'Depends on #N + #M + #K' reference clusters are honored."""
    body = "Depends on #2126 + #2127 + #2128 being deployed."
    assert policy.parse_depends_on_numbers(body) == {2126, 2127, 2128}


def test_parse_depends_on_prose_ends_reference_cluster():
    """Casual mentions after prose on the same line are not prerequisites."""
    body = "Depends on #10 and #11 being merged; see also #12 for audited reads."
    assert policy.parse_depends_on_numbers(body) == {10, 11}


def test_parse_depends_on_empty_body():
    """Empty body yields no prerequisites."""
    assert policy.parse_depends_on_numbers("") == set()


def test_body_depends_on_unresolved_true():
    """Declares a prerequisite that is still in the open set."""
    assert policy.body_depends_on_unresolved("Depends on #10", {10, 20}) is True


def test_body_depends_on_unresolved_false_when_resolved():
    """Declares a prerequisite that is not in the open set."""
    assert policy.body_depends_on_unresolved("Depends on #10", {20}) is False


def test_body_depends_on_unresolved_false_no_declaration():
    """No 'Depends on' means no blocking."""
    assert policy.body_depends_on_unresolved("Just a normal issue", {10}) is False


# ── Acceptance criterion 1: single open prerequisite excluded ──


def test_issue_with_one_open_prerequisite_excluded_from_intake():
    """Issue declaring 'Depends on #N' where #N is open is not a candidate."""
    prerequisite = {
        "number": 100,
        "state": "OPEN",
        "title": "Prerequisite",
        "labels": [{"name": "factory:unowned"}],
        "createdAt": "2026-08-16T00:00:00Z",
    }
    child = {
        "number": 200,
        "state": "OPEN",
        "title": "Child",
        "labels": [{"name": "factory:unowned"}],
        "body": "Depends on #100",
        "createdAt": "2026-08-16T01:00:00Z",
    }
    candidates = policy.build_candidates([prerequisite, child], [])
    assert all(c.number != 200 or c.kind != "issue" for c in candidates)


# ── Acceptance criterion 2: multiple prerequisites all must resolve ──


def test_issue_with_multiple_open_prerequisites_blocked_until_all_complete():
    """Issue blocked until every declared prerequisite is resolved."""
    prereq_a = {
        "number": 100,
        "state": "OPEN",
        "title": "Prereq A",
        "labels": [{"name": "factory:unowned"}],
        "createdAt": "2026-08-16T00:00:00Z",
    }
    prereq_b = {
        "number": 101,
        "state": "OPEN",
        "title": "Prereq B",
        "labels": [{"name": "factory:unowned"}],
        "createdAt": "2026-08-16T00:00:00Z",
    }
    child = {
        "number": 200,
        "state": "OPEN",
        "title": "Child",
        "labels": [{"name": "factory:unowned"}],
        "body": "Depends on #100 and #101",
        "createdAt": "2026-08-16T01:00:00Z",
    }
    # Both open — blocked
    candidates = policy.build_candidates([prereq_a, prereq_b, child], [])
    assert all(c.number != 200 or c.kind != "issue" for c in candidates)
    # One closed, one open — still blocked
    prereq_a_closed = {
        **prereq_a,
        "state": "CLOSED",
        "labels": [{"name": "factory:unowned"}, {"name": "ralph-status:done"}],
    }
    candidates = policy.build_candidates([prereq_a_closed, prereq_b, child], [])
    assert all(c.number != 200 or c.kind != "issue" for c in candidates)


# ── Acceptance criterion 3: closing final prerequisite makes child eligible ──


def test_closing_final_prerequisite_makes_child_eligible():
    """When the last prerequisite closes, the child becomes a candidate."""
    child = {
        "number": 200,
        "state": "OPEN",
        "title": "Child",
        "labels": [{"name": "factory:unowned"}],
        "body": "Depends on #100",
        "createdAt": "2026-08-16T01:00:00Z",
    }
    # Only closed prerequisite in the open set — no blocking
    candidates = policy.build_candidates([child], [])
    assert any(c.kind == "issue" and c.number == 200 for c in candidates)


# ── Acceptance criterion 4: casual mentions do not block ──


def test_casual_hash_mentions_do_not_block_intake():
    """Mentions like '#N' without 'Depends on' prefix are not prerequisites."""
    issue = {
        "number": 200,
        "state": "OPEN",
        "title": "Normal issue",
        "labels": [{"name": "factory:unowned"}],
        "body": "Related to #100 but not blocked by it.",
        "createdAt": "2026-08-16T01:00:00Z",
    }
    prerequisite_like = {
        "number": 100,
        "state": "OPEN",
        "title": "Some other issue",
        "labels": [{"name": "factory:unowned"}],
        "createdAt": "2026-08-16T00:00:00Z",
    }
    candidates = policy.build_candidates([prerequisite_like, issue], [])
    assert any(c.kind == "issue" and c.number == 200 for c in candidates)


def test_later_casual_mention_does_not_block_resolved_declaration():
    """A body that resolves its declared prereq stays eligible despite a
    later casual '#N' mention of an open issue (mirrors #2127/#2104)."""
    closed_prereq = {
        "number": 100,
        "state": "CLOSED",
        "title": "Closed prereq",
        "labels": [{"name": "ralph-status:done"}],
        "createdAt": "2026-08-16T00:00:00Z",
    }
    casual_open = {
        "number": 300,
        "state": "OPEN",
        "title": "Casually mentioned open issue",
        "labels": [{"name": "factory:unowned"}],
        "createdAt": "2026-08-16T00:00:00Z",
    }
    child = {
        "number": 200,
        "state": "OPEN",
        "title": "Child with resolved declaration",
        "labels": [{"name": "factory:unowned"}],
        "body": "Depends on #100.\n\nRelated cleanup tracked in #300 for context.",
        "createdAt": "2026-08-16T01:00:00Z",
    }
    candidates = policy.build_candidates([closed_prereq, casual_open, child], [])
    assert any(c.kind == "issue" and c.number == 200 for c in candidates)


# ── Acceptance criterion 5: existing canonical PR still repairable ──


def test_existing_pr_still_eligible_even_if_issue_gains_dependency():
    """A canonical PR for an issue can enter repair/review regardless of issue deps."""
    prereq = {
        "number": 100,
        "state": "OPEN",
        "title": "Prereq",
        "labels": [{"name": "factory:unowned"}],
        "createdAt": "2026-08-16T00:00:00Z",
    }
    issue = {
        "number": 200,
        "state": "OPEN",
        "title": "Child",
        "labels": [{"name": "factory:unowned"}],
        "body": "Depends on #100",
        "createdAt": "2026-08-16T01:00:00Z",
    }
    existing_pr = {
        "number": 300,
        "state": "OPEN",
        "isDraft": False,
        "labels": [
            {"name": "factory"},
            {"name": "factory:unowned"},
            {"name": "factory:review"},
        ],
        "headRefName": "factory/42-200-opencode-free",
        "body": "Worker: opencode-free-model-factory-42",
        "createdAt": "2026-08-16T02:00:00Z",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }
    candidates = policy.build_candidates([prereq, issue], [existing_pr])
    pr_candidates = [c for c in candidates if c.kind == "pr"]
    assert any(c.number == 300 for c in pr_candidates)


# ── Acceptance criterion 6: CBL chain represented in policy tests ──


def test_cbl_chain_2126_to_2129_all_open_blocks_youngest():
    """The full CBL dependency chain blocks the last child when all are open."""
    issues = [
        {
            "number": 2126,
            "state": "OPEN",
            "title": "Child A",
            "labels": [{"name": "factory:unowned"}],
            "createdAt": "2026-08-16T00:00:00Z",
        },
        {
            "number": 2127,
            "state": "OPEN",
            "title": "Child B",
            "labels": [{"name": "factory:unowned"}],
            "body": "Depends on #2126",
            "createdAt": "2026-08-16T01:00:00Z",
        },
        {
            "number": 2128,
            "state": "OPEN",
            "title": "Child C",
            "labels": [{"name": "factory:unowned"}],
            "body": "Depends on #2126 and #2127",
            "createdAt": "2026-08-16T02:00:00Z",
        },
        {
            "number": 2129,
            "state": "OPEN",
            "title": "Child D",
            "labels": [{"name": "factory:unowned"}],
            "body": "Depends on #2126, #2127, and #2128",
            "createdAt": "2026-08-16T03:00:00Z",
        },
    ]
    candidates = policy.build_candidates(issues, [])
    candidate_numbers = {c.number for c in candidates}
    # Child A (2126) has no deps — eligible
    assert 2126 in candidate_numbers
    # Children B, C, D have open prerequisites — blocked
    assert 2127 not in candidate_numbers
    assert 2128 not in candidate_numbers
    assert 2129 not in candidate_numbers


def test_cbl_chain_2126_to_2129_closing_a_and_b_unblocks_c():
    """When 2126 and 2127 close, 2128 becomes eligible while 2129 stays blocked."""
    issues = [
        {
            "number": 2126,
            "state": "CLOSED",
            "title": "Child A",
            "labels": [{"name": "ralph-status:done"}],
            "createdAt": "2026-08-16T00:00:00Z",
        },
        {
            "number": 2127,
            "state": "CLOSED",
            "title": "Child B",
            "labels": [{"name": "ralph-status:done"}],
            "createdAt": "2026-08-16T01:00:00Z",
        },
        {
            "number": 2128,
            "state": "OPEN",
            "title": "Child C",
            "labels": [{"name": "factory:unowned"}],
            "body": "Depends on #2126 and #2127",
            "createdAt": "2026-08-16T02:00:00Z",
        },
        {
            "number": 2129,
            "state": "OPEN",
            "title": "Child D",
            "labels": [{"name": "factory:unowned"}],
            "body": "Depends on #2126, #2127, and #2128",
            "createdAt": "2026-08-16T03:00:00Z",
        },
    ]
    candidates = policy.build_candidates(issues, [])
    candidate_numbers = {c.number for c in candidates}
    # 2128 now eligible (both prereqs closed, only child left is open)
    assert 2128 in candidate_numbers
    # 2129 still blocked by open 2128
    assert 2129 not in candidate_numbers


def test_pr_below_no_diff_retry_limit_remains_selectable():
    """A truthful changes-requested PR is still executable before retry exhaustion."""
    target = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
    )
    candidates = policy.build_candidates(
        [],
        [target],
        no_diff_attempts_by_issue={2264: policy.FACTORY_NO_DIFF_RETRY_LIMIT - 1},
    )
    assert any(item.kind == "pr" and item.number == 2264 for item in candidates)


def test_pr_at_no_diff_retry_limit_is_excluded_without_blocked_label():
    """Retry exhaustion suppresses PR candidacy without falsifying workflow labels."""
    target = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
    )
    candidates = policy.build_candidates(
        [],
        [target],
        no_diff_attempts_by_issue={2264: policy.FACTORY_NO_DIFF_RETRY_LIMIT},
    )
    assert candidates == []
    assert label_names(target) == {
        "factory",
        "factory:unowned",
        "factory:changes-requested",
    }


def test_review_pr_at_no_diff_retry_limit_is_excluded_without_blocked_label():
    """A review PR at the retry limit stays factory:review while becoming ineligible."""
    target = pr_fixture(
        number=2271,
        issue=2201,
        labels=["factory", "factory:unowned", "factory:review"],
    )
    candidates = policy.build_candidates(
        [],
        [target],
        no_diff_attempts_by_issue={2271: policy.FACTORY_NO_DIFF_RETRY_LIMIT},
    )
    assert candidates == []
    names = label_names(target)
    assert "factory:review" in names
    assert "factory:blocked" not in names


def test_pr_no_diff_retry_suppression_expires_with_reset_window():
    """Aged no-diff markers fall out of the rolling window and the PR is selectable."""
    now = 2_000_000_000
    expired = now - policy.FACTORY_NO_DIFF_RETRY_RESET_SECONDS - 1
    comments = [
        {
            "author_association": "OWNER",
            "body": (
                "<!-- comic-pile-factory-claim-released-v3:pr-2264:worker:"
                f"{expired}:repair-no-persisted-change-handoff -->"
            ),
        }
        for _ in range(policy.FACTORY_NO_DIFF_RETRY_LIMIT)
    ]
    counts = policy.no_diff_attempts_from_comments(comments, now_epoch=now)
    assert counts == {}
    target = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
    )
    candidates = policy.build_candidates([], [target], no_diff_attempts_by_issue=counts)
    assert any(item.kind == "pr" and item.number == 2264 for item in candidates)


def test_recent_pr_no_diff_markers_exclude_candidate():
    """Trusted in-window PR no-diff markers consume the retry budget."""
    now = 2_000_000_000
    head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    comments = [
        {
            "author_association": "OWNER",
            "body": (
                "<!-- comic-pile-factory-claim-released-v3:pr-2264:worker:"
                f"{now - offset}:repair-no-persisted-change-handoff"
                f":sha={head}:stage=factory:changes-requested:conflicted=0 -->"
            ),
        }
        for offset in range(policy.FACTORY_NO_DIFF_RETRY_LIMIT)
    ]
    attempts = policy.parse_no_diff_attempts_from_comments(comments, now_epoch=now)
    assert len(attempts) >= policy.FACTORY_NO_DIFF_RETRY_LIMIT
    target = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
        head=head,
    )
    candidates = policy.build_candidates([], [target], no_diff_attempt_records=attempts)
    assert candidates == []


def test_issue_no_diff_retry_suppression_remains_intact():
    """Issue candidacy still uses the same bounded no-diff retry counter."""
    target = issue_fixture(31)
    below = policy.build_candidates(
        [target],
        [],
        no_diff_attempts_by_issue={31: policy.FACTORY_NO_DIFF_RETRY_LIMIT - 1},
    )
    exhausted = policy.build_candidates(
        [target],
        [],
        no_diff_attempts_by_issue={31: policy.FACTORY_NO_DIFF_RETRY_LIMIT},
    )
    assert any(item.kind == "issue" and item.number == 31 for item in below)
    assert not any(item.kind == "issue" and item.number == 31 for item in exhausted)


def _generation_attempts(
    *,
    number: int = 2264,
    sha: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    stage: str = "factory:changes-requested",
    conflicted: bool = False,
    now: int = 2_000_000_000,
):
    comments = [
        {
            "author_association": "OWNER",
            "body": (
                "<!-- comic-pile-factory-claim-released-v3:"
                f"pr-{number}:worker:{now - offset}:repair-no-persisted-change-handoff"
                f":sha={sha}:stage={stage}:conflicted={int(conflicted)} -->"
            ),
        }
        for offset in range(policy.FACTORY_NO_DIFF_RETRY_LIMIT)
    ]
    return policy.parse_no_diff_attempts_from_comments(comments, now_epoch=now)


def test_exhausted_pr_becomes_selectable_after_new_head():
    """A new commit starts a fresh retry budget immediately."""
    old_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    new_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    attempts = _generation_attempts(sha=old_head)
    unchanged = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
        head=old_head,
    )
    pushed = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
        head=new_head,
    )
    assert policy.build_candidates([], [unchanged], no_diff_attempt_records=attempts) == []
    candidates = policy.build_candidates([], [pushed], no_diff_attempt_records=attempts)
    assert any(item.kind == "pr" and item.number == 2264 for item in candidates)


def test_exhausted_pr_becomes_selectable_after_new_actionable_review_state():
    """New review findings on the same head immediately wake the PR."""
    head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    attempts = _generation_attempts(sha=head, stage="factory:review")
    still_review = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:review"],
        head=head,
    )
    newly_actionable = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
        head=head,
    )
    assert policy.build_candidates([], [still_review], no_diff_attempt_records=attempts) == []
    candidates = policy.build_candidates(
        [],
        [newly_actionable],
        no_diff_attempt_records=attempts,
    )
    assert any(item.kind == "pr" and item.number == 2264 for item in candidates)


def test_exhausted_pr_becomes_selectable_after_new_merge_conflict():
    """A newly developed merge conflict is new work and must be selectable."""
    head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    attempts = _generation_attempts(sha=head, conflicted=False)
    still_clean = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
        head=head,
    )
    newly_conflicted = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
        head=head,
        mergeable="CONFLICTING",
        merge_state="DIRTY",
    )
    assert policy.build_candidates([], [still_clean], no_diff_attempt_records=attempts) == []
    candidates = policy.build_candidates(
        [],
        [newly_conflicted],
        no_diff_attempt_records=attempts,
    )
    assert any(item.kind == "pr" and item.number == 2264 for item in candidates)


def test_legacy_unscoped_pr_markers_do_not_suppress_known_head():
    """Number-only historical markers must not recreate the scheduler oubliette."""
    now = 2_000_000_000
    comments = [
        {
            "author_association": "OWNER",
            "body": (
                "<!-- comic-pile-factory-claim-released-v3:pr-2264:worker:"
                f"{now - offset}:repair-no-persisted-change-handoff -->"
            ),
        }
        for offset in range(policy.FACTORY_NO_DIFF_RETRY_LIMIT)
    ]
    attempts = policy.parse_no_diff_attempts_from_comments(comments, now_epoch=now)
    target = pr_fixture(
        number=2264,
        issue=2200,
        labels=["factory", "factory:unowned", "factory:changes-requested"],
    )
    candidates = policy.build_candidates([], [target], no_diff_attempt_records=attempts)
    assert any(item.kind == "pr" and item.number == 2264 for item in candidates)


# ── Issue #2164: duplicate-intake suppression for open PRs closing an issue ──
#
# A human-authored PR that explicitly closes an issue owns that issue even when
# it lives on a local/* branch without factory provenance. Suppression must not
# depend on branch shape or provenance; only an explicit closing reference and
# an open (not superseded) PR count.


def human_pr_fixture(
    *,
    number: int,
    body: str,
    title: str = "",
    branch: str = "local/2127-cbl-commit",
    state: str = "OPEN",
    draft: bool = False,
) -> dict[str, object]:
    """Build a non-factory PR payload for duplicate-intake suppression tests."""
    return {
        "number": number,
        "state": state,
        "isDraft": draft,
        "labels": [],
        "headRefName": branch,
        "headRefOid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "body": body,
        "title": title,
        "createdAt": "2026-09-05T00:00:00Z",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }


def test_human_author_open_pr_with_closes_keyword_suppresses_intake():
    """A human ``local/*`` PR with ``Closes #N`` owns the issue (#2164)."""
    issue = issue_fixture(2127)
    human_pr = human_pr_fixture(
        number=2161,
        body="Closes #2127.\n\nImplements the complete CBL chain.",
        branch="local/2127-cbl-commit",
    )

    candidates = policy.build_candidates([issue], [human_pr])

    assert all(
        candidate.number != 2127 or candidate.kind != "issue"
        for candidate in candidates
    )
    assert candidates == []


def test_non_factory_branch_closing_reference_is_recognized():
    """``linked_issue_from_pr`` resolves explicit body closing references."""
    pr = human_pr_fixture(
        number=2161,
        body="Fixes #2127",
        branch="local/2127-cbl-commit",
    )
    assert policy.linked_issue_from_pr(pr) == 2127
    assert policy.issue_explicitly_closed_by_pr(pr) == 2127


def test_factory_canonical_pr_is_still_recognized():
    """Factory branches retain canonical identity without body keywords."""
    pr = pr_fixture(
        number=1512,
        issue=1487,
        labels=["factory", "factory:18", "factory:review"],
    )
    assert policy.linked_issue_from_pr(pr) == 1487
    assert policy.pr_suppresses_issue_candidate(pr, {})
    candidates = policy.build_candidates([issue_fixture(1487)], [pr])
    assert candidates == []


def test_pr_mentioning_hash_without_closing_reference_does_not_suppress():
    """Stacked/child PRs that only mention ``#N`` never block fresh intake."""
    issue = issue_fixture(2127)
    mention_pr = human_pr_fixture(
        number=2163,
        body="Stacked on #2127; details in the parent PR.",
        title="Extended queue notes",
    )
    assert policy.linked_issue_from_pr(mention_pr) is None
    assert policy.issue_explicitly_closed_by_pr(mention_pr) is None

    candidates = policy.build_candidates([issue], [mention_pr])

    assert [(candidate.kind, candidate.number) for candidate in candidates] == [
        ("issue", 2127)
    ]


def test_pr_casual_title_hash_without_fix_keyword_does_not_suppress():
    """A title that merely references ``#N`` is not a closing claim."""
    issue = issue_fixture(2127)
    pr = human_pr_fixture(
        number=2164,
        title="Backend work shared with #2127",
        body="No closing reference here.",
    )
    assert policy.linked_issue_from_pr(pr) is None

    candidates = policy.build_candidates([issue], [pr])

    assert any(candidate.kind == "issue" and candidate.number == 2127 for candidate in candidates)


def test_closed_human_pr_does_not_suppress_new_implementation():
    """A closed or superseded PR releases its issue to the queue again."""
    issue = issue_fixture(2127)
    closed_pr = human_pr_fixture(
        number=2162,
        body="Closes #2127.",
        state="CLOSED",
    )
    assert not policy.pr_suppresses_issue_candidate(closed_pr, {})

    candidates = policy.build_candidates([issue], [closed_pr])

    assert [(candidate.kind, candidate.number) for candidate in candidates] == [
        ("issue", 2127)
    ]


def test_human_pr_suppresses_intake_without_becoming_factory_work():
    """Suppression is intake-only; the human PR's own lifecycle is untouched."""
    issue = issue_fixture(2127)
    human_pr = human_pr_fixture(number=2161, body="Closes #2127.")

    candidates = policy.build_candidates([issue], [human_pr])

    assert candidates == []
    assert not policy.pr_is_static_candidate(human_pr, {2127: issue})


def test_concurrent_intake_cannot_assign_duplicate_implementation():
    """A batch of workers never assigns fresh work for an already-owned issue."""
    issue = issue_fixture(2127)
    human_pr = human_pr_fixture(number=2161, body="Closes #2127.")
    other = issue_fixture(2199)

    candidates = policy.build_candidates([issue, other], [human_pr])
    assignments = policy.plan_distinct_assignments(
        candidates,
        ["29", "30", "31"],
    )

    assigned = [candidate.number for candidate in assignments.values()]
    assert 2127 not in assigned
    assert 2199 in assigned


def test_issue_explicitly_closed_by_pr_recognizes_all_closing_inflections():
    """Every GitHub closing keyword form is recognized case-insensitively."""
    for body in (
        "Closes #2127",
        "Fixes #2127",
        "Resolves #2127",
        "closed #2127",
        "fixed #2127",
        "Fixed #2127 and #2128",
    ):
        pr = human_pr_fixture(number=2161, body=body)
        assert policy.issue_explicitly_closed_by_pr(pr) == 2127, body
        assert policy.linked_issue_from_pr(pr) == 2127, body
    casual = human_pr_fixture(number=2161, body="Related to #2127; see parent.")
    assert policy.issue_explicitly_closed_by_pr(casual) is None
