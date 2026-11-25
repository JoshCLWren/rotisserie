"""The assigned review task: how to end, when to skip, and what approval does."""

from __future__ import annotations

from rotisserie.factory_console.service import assign_task


def test_review_task_asks_for_one_verdict_line_and_warns_that_approval_merges() -> None:
    text = " ".join(assign_task.STEPS["review"])
    assert "`Verdict: changes-requested`" in text
    assert "`Verdict: approve`" in text
    assert "**Comment**" in text
    assert "An approval merges this pull request." in text
    assert "on this pull request's current head commit" in text
