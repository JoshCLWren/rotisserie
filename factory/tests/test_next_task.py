"""Tests for GitHub issue selection."""

from unittest.mock import Mock

import pytest

import scripts.next_task as next_task
from scripts.next_task import _issue_context, select_next


def _issue(number: int, title: str, labels: list[str], body: str = "") -> dict:
    """Build the subset of GitHub issue data used by the selector."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": label} for label in labels],
        "url": f"https://github.com/example/repo/issues/{number}",
    }


def test_select_next_prefers_highest_priority_pending_issue() -> None:
    """The selector should choose high priority work before medium work."""
    issues = [
        _issue(20, "Medium", ["ralph-task", "ralph-status:pending", "ralph-priority:medium"]),
        _issue(10, "High", ["ralph-task", "ralph-status:pending", "ralph-priority:high"]),
    ]

    candidate = select_next(issues, set())

    assert candidate is not None
    assert candidate.issue["number"] == 10


def test_select_next_ignores_non_pending_and_parent_issues() -> None:
    """Only executable pending child tasks should be selected."""
    issues = [
        _issue(1, "Epic", ["epic", "ralph-status:pending", "ralph-priority:critical"]),
        _issue(2, "PRD", ["prd", "ralph-status:pending", "ralph-priority:critical"]),
        _issue(3, "Active", ["ralph-task", "ralph-status:in-progress", "ralph-priority:high"]),
        _issue(4, "Ready", ["ralph-task", "ralph-status:pending", "ralph-priority:low"]),
    ]

    candidate = select_next(issues, set())

    assert candidate is not None
    assert candidate.issue["number"] == 4


def test_select_next_ignores_manual_only_issue() -> None:
    """A manual-only issue must never enter autonomous selection."""
    issues = [
        _issue(
            10,
            "Production acceptance gate",
            ["ralph-task", "ralph-status:pending", "ralph-priority:critical"],
            "<!-- factory-execution:manual-only -->\nHuman-controlled gate.",
        ),
        _issue(20, "Ready task", ["ralph-task", "ralph-status:pending", "ralph-priority:low"]),
    ]

    candidate = select_next(issues, set())

    assert candidate is not None
    assert candidate.issue["number"] == 20


def test_select_next_ignores_body_declared_acceptance_parent() -> None:
    """A #1615-shaped parent (contract in body, no epic/prd label) is not executable."""
    issues = [
        _issue(
            10,
            "CBL browser and adoption workflow",
            ["ralph-task", "ralph-status:pending", "ralph-priority:critical"],
            "This is the acceptance parent for CBL adoption.\n"
            "- [x] #2127 — transactional adoption\n"
            "- [ ] #2128 — production browser UI\n",
        ),
        _issue(20, "Ready task", ["ralph-task", "ralph-status:pending", "ralph-priority:low"]),
    ]

    candidate = select_next(issues, set())

    assert candidate is not None
    assert candidate.issue["number"] == 20


def test_select_next_keeps_deferred_production_acceptance_executable() -> None:
    """An issue that hands operator acceptance to another issue stays executable.

    #3037, #2718, and #2128 are executable implementation work that explicitly
    defers the operator acceptance pass. Classifying them as acceptance parents
    would drop a user-reported bug from the first delivery queue.
    """
    issues = [
        _issue(
            10,
            "Enforce one membership per canonical issue",
            ["ralph-task", "ralph-status:pending", "ralph-priority:critical"],
            "## Production acceptance split\n"
            "#3042 owns the production-wide audit and reconciliation.\n"
            "- [ ] A Reading Plan cannot persist the same issue twice.\n"
            "- [ ] The invariant is enforced at the database layer.\n",
        ),
        _issue(
            11,
            "CBL adoption browser UI",
            ["ralph-task", "ralph-status:pending", "ralph-priority:high"],
            "Refs #2129 so it can use this path for production acceptance.\n"
            "- [ ] Reader adopts after one compact confirmation\n",
        ),
    ]

    candidate = select_next(issues, set())

    assert candidate is not None
    assert candidate.issue["number"] == 10


def test_select_next_keeps_casual_acceptance_mention_executable() -> None:
    """A casual 'production acceptance' mention without criteria stays executable."""
    issues = [
        _issue(
            10,
            "Fix roll boundary",
            ["ralph-task", "ralph-status:pending", "ralph-priority:critical"],
            "Verify in production acceptance later; no subtasks.",
        ),
    ]

    candidate = select_next(issues, set())

    assert candidate is not None
    assert candidate.issue["number"] == 10


def test_select_next_skips_issue_with_open_dependency() -> None:
    """An issue should wait until its referenced dependency is closed."""
    issues = [
        _issue(
            20,
            "Blocked task",
            ["ralph-task", "ralph-status:pending", "ralph-priority:critical"],
            "Depends on #19",
        ),
        _issue(21, "Ready task", ["ralph-task", "ralph-status:pending", "ralph-priority:high"]),
    ]

    candidate = select_next(issues, set())

    assert candidate is not None
    assert candidate.issue["number"] == 21


def test_select_next_allows_closed_dependency() -> None:
    """A pending issue becomes eligible when its dependency is closed."""
    issue = _issue(
        20,
        "Unblocked task",
        ["ralph-task", "ralph-status:pending", "ralph-priority:high"],
        "Depends on #19",
    )

    candidate = select_next([issue], {19})

    assert candidate is not None
    assert candidate.issue["number"] == 20


def test_issue_context_reports_scope_dependencies_and_files() -> None:
    """The selector output should provide enough bounded context to begin work."""
    issue = _issue(
        42,
        "Document workflow",
        ["ralph-task", "ralph-status:pending", "ralph-priority:high"],
        """## Goal

Make agents productive.

Depends on #41.

Update `scripts/next_task.py` and `docs/ISSUE_EXECUTION_PROTOCOL.md`.
""",
    )

    context = _issue_context(issue, {41})

    assert "Make agents productive." in context
    assert "Dependencies: #41 (closed)" in context
    assert "scripts/next_task.py" in context
    assert "Required verification:" in context


def test_start_task_updates_label_and_posts_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid task should update its label and post the start comment."""
    monkeypatch.setattr(
        next_task,
        "_gh_issue",
        lambda issue_number: _issue(
            issue_number,
            "Ready task",
            ["ralph-task", "ralph-status:pending", "ralph-priority:high"],
        ),
    )
    monkeypatch.setattr(next_task, "_gh_issue_list", lambda state: [])
    gh_runner = Mock(return_value="")
    monkeypatch.setattr(next_task, "_run_gh", gh_runner)

    assert next_task._start_task(42) == 0

    assert gh_runner.call_count == 2
    assert gh_runner.call_args_list[0].args[0][1:3] == ["issue", "edit"]
    assert gh_runner.call_args_list[1].args[0][1:3] == ["issue", "comment"]


def test_start_task_rejects_non_pending_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task that is not pending must not trigger GitHub writes."""
    monkeypatch.setattr(
        next_task,
        "_gh_issue",
        lambda issue_number: _issue(
            issue_number,
            "Active task",
            ["ralph-task", "ralph-status:in-progress", "ralph-priority:high"],
        ),
    )
    gh_runner = Mock()
    monkeypatch.setattr(next_task, "_run_gh", gh_runner)

    with pytest.raises(RuntimeError, match="is not pending"):
        next_task._start_task(42)

    gh_runner.assert_not_called()


def test_start_task_rejects_parent_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit start cannot bypass the parent-selection fence."""
    monkeypatch.setattr(
        next_task,
        "_gh_issue",
        lambda issue_number: _issue(
            issue_number,
            "Parent epic",
            ["ralph-task", "ralph-status:pending", "ralph-priority:high", "epic"],
        ),
    )
    gh_runner = Mock()
    monkeypatch.setattr(next_task, "_run_gh", gh_runner)

    with pytest.raises(RuntimeError, match="parent PRD/epic"):
        next_task._start_task(42)

    gh_runner.assert_not_called()


def test_start_task_rejects_manual_only_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit start cannot bypass the manual-only marker."""
    monkeypatch.setattr(
        next_task,
        "_gh_issue",
        lambda issue_number: _issue(
            issue_number,
            "Human gate",
            ["ralph-task", "ralph-status:pending", "ralph-priority:high"],
            "<!-- factory-execution:manual-only -->",
        ),
    )
    gh_runner = Mock()
    monkeypatch.setattr(next_task, "_run_gh", gh_runner)

    with pytest.raises(RuntimeError, match="manual-only"):
        next_task._start_task(42)

    gh_runner.assert_not_called()


def test_start_task_rejects_body_declared_acceptance_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit start cannot bypass the body-declared acceptance-parent fence."""
    monkeypatch.setattr(
        next_task,
        "_gh_issue",
        lambda issue_number: _issue(
            issue_number,
            "CBL browser and adoption workflow",
            ["ralph-task", "ralph-status:pending", "ralph-priority:high"],
            "This is the acceptance parent for CBL adoption.\n"
            "- [x] #2127 — transactional adoption\n"
            "- [ ] #2128 — production browser UI\n",
        ),
    )
    gh_runner = Mock()
    monkeypatch.setattr(next_task, "_run_gh", gh_runner)

    with pytest.raises(RuntimeError, match="product-acceptance parent"):
        next_task._start_task(42)

    gh_runner.assert_not_called()


def test_start_task_allows_deferred_production_acceptance_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An issue that defers the operator acceptance pass stays startable."""
    monkeypatch.setattr(
        next_task,
        "_gh_issue",
        lambda issue_number: _issue(
            issue_number,
            "Enforce one membership per canonical issue",
            ["ralph-task", "ralph-status:pending", "ralph-priority:high"],
            "## Production acceptance split\n"
            "#3042 owns the production-wide audit and reconciliation.\n"
            "- [ ] A Reading Plan cannot persist the same issue twice.\n",
        ),
    )
    gh_runner = Mock()
    monkeypatch.setattr(next_task, "_run_gh", gh_runner)
    monkeypatch.setattr(next_task, "_gh_issue_list", lambda state: [])
    monkeypatch.setattr(next_task, "_issue_context", lambda issue, closed: "context")

    next_task._start_task(42)

    gh_runner.assert_called()


def test_start_task_rejects_unresolved_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task with an open dependency must not update its status."""
    monkeypatch.setattr(
        next_task,
        "_gh_issue",
        lambda issue_number: _issue(
            issue_number,
            "Blocked task",
            ["ralph-task", "ralph-status:pending", "ralph-priority:high"],
            "Depends on #41",
        ),
    )
    monkeypatch.setattr(next_task, "_gh_issue_list", lambda state: [])
    gh_runner = Mock()
    monkeypatch.setattr(next_task, "_run_gh", gh_runner)

    with pytest.raises(RuntimeError, match="unresolved dependency"):
        next_task._start_task(42)

    gh_runner.assert_not_called()


def test_start_task_succeeds_when_comment_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A completed label transition remains successful if commenting fails."""
    monkeypatch.setattr(
        next_task,
        "_gh_issue",
        lambda issue_number: _issue(
            issue_number,
            "Ready task",
            ["ralph-task", "ralph-status:pending", "ralph-priority:high"],
        ),
    )
    monkeypatch.setattr(next_task, "_gh_issue_list", lambda state: [])

    gh_runner = Mock()

    def run_github(command: list[str], failure_message: str) -> str:
        if command[1:3] == ["issue", "comment"]:
            raise RuntimeError("temporary GitHub outage")
        return ""

    gh_runner.side_effect = run_github
    monkeypatch.setattr(next_task, "_run_gh", gh_runner)

    assert next_task._start_task(42) == 0

    assert gh_runner.call_count == 2
    assert "label updated, but comment failed" in capsys.readouterr().err
