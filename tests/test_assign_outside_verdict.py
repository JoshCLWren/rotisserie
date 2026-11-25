"""The assigned task follows the owner's adopted outside-worker prompt."""

from __future__ import annotations

from rotisserie.factory_console.service import assign_task


def test_review_task_matches_the_verdict_and_independence_rules() -> None:
    text = " ".join(assign_task.STEPS["review"])
    assert "`Verdict: approve`" in text
    assert "`Verdict: changes-requested` or `Verdict: approve`" in text
    assert "names the reviewed head commit SHA" in text
    assert "An approval merges this pull request." in text
    assert "required CI is green at the current head" in text
    assert "another session of the same model is not independent" in text
    assert "`factory:parked`" in text
    assert "**Comment**" in text


def test_incident_implementation_refers_instead_of_closing() -> None:
    assert "`Refs {ref}`" in " ".join(assign_task.STEPS["implement"])


def test_every_model_line_step_explains_an_unexposed_model() -> None:
    for kind in ("implement", "review", "review-findings", "ci-repair"):
        assert "`Model: <tool name> (model unavailable)`" in " ".join(assign_task.STEPS[kind]), kind
