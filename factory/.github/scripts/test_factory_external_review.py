"""Outside review recording (issue #191): only an independent model's review counts."""

from __future__ import annotations

import types

import factory_external_review as fe
import factory_review_policy as policy
import pytest

HEAD = "b" * 40
ACTIONS = {"GITHUB_ACTIONS": "true"}


class Ctrl(types.SimpleNamespace):
    def __init__(self, trailers: list[str], author_markers: list[str] | None = None) -> None:
        super().__init__()
        self._review_policy = policy
        self.REPO = "acme/widgets"
        self.trailers = trailers
        self.author_markers = author_markers or []
        self.posted: list[str] = []
        self.labels: list[tuple[int, str, str]] = []
        self.repairs: list[dict[str, object]] = []

    def gh_json(self, args):
        return [[{"commit": {"message": f"work\n\nModel: {m}"}} for m in self.trailers]]

    def pr_json(self, number):
        return {
            "headRefOid": HEAD,
            "state": "OPEN",
            "isDraft": False,
            "labels": [],
            "headRefName": "x",
        }

    def labels_of(self, pr):
        return set()

    def active_factory_owner(self, labels):
        return None

    def model_comment_bodies(self, number):
        return self.author_markers

    def persist_repair_handoff(self, **kwargs):
        self.repairs.append(kwargs)


def review(state: str = "APPROVED", model: str = "claude-opus-5-5", actor: str = "alice") -> dict:
    return {
        "user": {"login": actor},
        "commit_id": HEAD,
        "state": state,
        "body": f"Looks right.\n\nModel: {model}",
    }


@pytest.fixture
def ctrl(monkeypatch: pytest.MonkeyPatch):
    def make(*trailers: str, markers: list[str] | None = None) -> Ctrl:
        fake = Ctrl(list(trailers), markers)
        monkeypatch.setattr(fe, "_controller", fake)
        return fake

    return make


def test_review_by_a_model_that_wrote_a_commit_is_refused(ctrl) -> None:
    ctrl("gpt-5.2", "claude-opus-5-5")
    with pytest.raises(ValueError, match="authored this PR"):
        fe.record(pr_number=4, head=HEAD, actor="alice", review=review(), environment=ACTIONS)


def test_review_by_a_recorded_author_model_is_refused(ctrl) -> None:
    marker = policy.agent_model_author_marker(pr=4, head=HEAD, model="claude-opus-5-5", agent="9")
    ctrl("gpt-5.2", markers=[marker])
    with pytest.raises(ValueError, match="authored this PR"):
        fe.record(pr_number=4, head=HEAD, actor="alice", review=review(), environment=ACTIONS)


def test_independent_request_changes_routes_to_repair(ctrl) -> None:
    fake = ctrl("gpt-5.2")
    result = fe.record(
        pr_number=4,
        head=HEAD,
        actor="alice",
        review=review("CHANGES_REQUESTED"),
        environment=ACTIONS,
    )
    assert result["next"] == "factory:changes-requested"
    assert fake.repairs[0]["reviewer"] == "external-alice"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"user": {"login": "bob"}}, "not written by the claimant"),
        ({"commit_id": "c" * 40}, "not on the claimed head"),
        ({"state": "COMMENTED"}, "Approve or Request changes"),
        ({"body": "no model line"}, "exactly one 'Model:"),
    ],
)
def test_review_shape_is_checked_before_anything_is_written(ctrl, change, message) -> None:
    fake = ctrl("gpt-5.2")
    with pytest.raises(ValueError, match=message):
        fe.record(
            pr_number=4,
            head=HEAD,
            actor="alice",
            review={**review(), **change},
            environment=ACTIONS,
        )
    assert fake.posted == [] and fake.repairs == []


def test_recording_needs_trusted_actions(ctrl) -> None:
    ctrl("gpt-5.2")
    with pytest.raises(RuntimeError, match="trusted Actions"):
        fe.record(pr_number=4, head=HEAD, actor="alice", review=review(), environment={})


def test_external_reviewer_ids_parse_as_trusted_approvals() -> None:
    identity = fe.reviewer_identity("Some_Agent.Bot")
    assert identity == "external-some-agent-bot"
    marker = policy.review_marker(
        pr=4, head=HEAD, reviewer=identity, producer=None, verdict="approve"
    )
    assert policy.REVIEW_MARKER_RE.fullmatch(marker)
    assert policy.current_head_approvers([marker], pr=4, head=HEAD) == {identity}
