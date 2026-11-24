"""Owner review policy: a model known to have authored a PR must not approve it."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "free-model-factory-run.yml"
sys.path.insert(0, str(SCRIPTS))
policy = importlib.import_module("factory_review_policy")
guard = importlib.import_module("factory_review_model_guard")

HEAD = "a" * 40
OLD_HEAD = "b" * 40


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("opencode-go/hy4-flash-free", "hy4-flash"),
        ("opencode-go/hy4-flash-free", "vendor/hy4-flash:free"),
        ("opencode-go+hy4-flash-free", "zen/hy4-flash"),
        # Legacy marker token: the route was sanitized into the name.
        ("opencode-go-hy4-flash-free", "hy4-flash"),
        ("cursor-grok-4.6-high-fast", "grok-4.6-low"),
        ("Z-AI/GLM-4.7-Flash", "z-ai+glm-4.7-flash"),
    ],
)
def test_aliases_of_one_model_are_the_same_actual_model(left: str, right: str) -> None:
    assert policy.same_actual_model(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("zai/glm-4.6", "zai/glm-4.5"),  # same family, different models
        ("hy4-flash", "hy4-pro"),
        ("qwen3-coder", "qwen3-coder-plus"),
        ("deepseek-v4-flash", "hy4-flash"),  # a shared bare word is not identity
        ("", "glm-4.6"),
    ],
)
def test_genuinely_different_models_are_distinct(left: str, right: str) -> None:
    assert not policy.same_actual_model(left, right)


def _author(model: str, head: str = HEAD, agent: str = "43") -> str:
    return policy.agent_model_author_marker(pr=7, head=head, model=model, agent=agent)


def _review(model: str, agent: str = "17", head: str = HEAD) -> str:
    return policy.reviewer_model_marker(pr=7, head=head, reviewer_model=model, reviewer_agent=agent)


def test_author_from_an_earlier_head_still_cannot_approve_after_a_sync() -> None:
    """A sync head with no author marker never launders the earlier author away."""
    comments = [_author("opencode-go/hy4-flash-free", head=OLD_HEAD), _review("hy4-flash")]
    verdict = policy.evaluate_review_eligibility(comments, pr=7, head=HEAD, approvers={"17"})
    assert not verdict.eligible
    assert verdict.status == "self-review"
    assert "authored this PR" in verdict.reason


def test_every_known_author_is_excluded_and_another_model_counts() -> None:
    comments = [
        _author("zai/glm-4.6", head=OLD_HEAD, agent="40"),
        _author("opencode-go/hy4-flash-free", agent="43"),
        _review("zai/glm-4.6", agent="17"),
    ]
    assert policy.evaluate_review_eligibility(comments, pr=7, head=HEAD).status == "self-review"
    comments.append(_review("mistral/devstral-2", agent="18"))
    verdict = policy.evaluate_review_eligibility(comments, pr=7, head=HEAD)
    assert verdict.eligible
    assert verdict.qualifying == ("mistral+devstral-2",)
    assert verdict.self_reviews == ("zai+glm-4.6",)


def test_external_or_human_pr_may_be_reviewed_by_any_model() -> None:
    human = policy.agent_model_author_marker(pr=7, head=HEAD, model="human", agent="interactive-o")
    for comments in ([], [human]):
        verdict = policy.evaluate_review_eligibility(comments, pr=7, head=HEAD, approvers={"17"})
        assert verdict.eligible
        assert verdict.author_models == ()


def test_known_author_with_unrecorded_reviewer_goes_back_to_review() -> None:
    verdict = policy.evaluate_review_eligibility(
        [_author("zai/glm-4.6")], pr=7, head=HEAD, approvers={"17"}
    )
    assert not verdict.eligible
    assert verdict.status == "reviewer-model-unrecorded"


def test_records_for_another_head_or_pr_do_not_count() -> None:
    comments = [_review("zai/glm-4.6", head=OLD_HEAD)]
    assert policy.evaluate_review_eligibility(comments, pr=7, head=HEAD).status == "no-approval"
    other = policy.reviewer_model_marker(
        pr=8, head=HEAD, reviewer_model="zai/glm-4.6", reviewer_agent="17"
    )
    assert policy.evaluate_review_eligibility([other], pr=7, head=HEAD).status == "no-approval"


def test_untrusted_comment_streams_cannot_supply_records() -> None:
    rows = [
        {"user": {"login": "someone"}, "author_association": "OWNER", "body": _author("glm-4.6")},
        {"user": {"login": "github-actions[bot]"}, "body": _author("hy4-flash")},
    ]
    assert guard.author_models_from_records([rows], pr=7) == ["hy4-flash"]


def test_guard_check_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    authors = json.dumps(["opencode-go+hy4-flash-free"])
    assert guard.main(["check", "--authors-json", authors, "--model", "zen/hy4-flash"]) == 0
    assert "authored this PR" in capsys.readouterr().out
    assert guard.main(["check", "--authors-json", authors, "--model", "zai/glm-4.6"]) == 1
    assert guard.main(["check", "--authors-json", "[]", "--model", "zai/glm-4.6"]) == 1
    assert guard.main(["check", "--authors-json", "{}", "--model", "x"]) == 2


def test_run_workflow_reads_authors_once_and_reselects_without_more_calls() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("factory_review_model_guard.py authors") == 1
    assert "review_authors: ${{ steps.prepare.outputs.review_authors }}" in text
    selector = text.split("- name: Select execution candidate at dispatch time", 1)[1]
    selector = selector.split("\n      - name:", 1)[0]
    assert "factory_review_model_guard.py check" in selector
    assert "--attempted-pair" in selector
    assert 'if [[ "$REQUESTED_MODEL" == auto ]]; then' in selector
    # The guard runs before the selection is published to later steps.
    assert selector.index("factory_review_model_guard.py check") < selector.index(
        'echo "runtime_model=$runtime_model"'
    )


def test_provider_pinned_reviewer_repick_stays_on_that_provider() -> None:
    """An operator-pinned provider with an automatic model never crosses the fleet."""
    selector = WORKFLOW.read_text(encoding="utf-8").split(
        "- name: Select execution candidate at dispatch time", 1
    )[1].split("\n      - name:", 1)[0]
    guard_block = selector.split("factory_review_model_guard.py check", 1)[1].split("done", 1)[0]
    pinned = guard_block.split('if [[ "$REQUESTED_PROVIDER" == auto ]]; then', 1)[1]
    fleet, own = pinned.split("else", 1)
    assert "--eligible-sources-file" in fleet
    own_branch = own.split("fi", 1)[0]
    assert "--eligible-sources-file" not in own_branch
    assert '--attempted "$model"' in own_branch
