"""Outside review recording (issue #191): only an independent model's review counts."""

from __future__ import annotations

import types

import factory_external_review as fe
import factory_review_exclusion as exclusion
import factory_review_policy as policy
import pytest

HEAD = "b" * 40
OLDER = "a" * 40
ACTIONS = {"GITHUB_ACTIONS": "true"}


class Ctrl(types.SimpleNamespace):
    def __init__(
        self,
        trailers: list[str],
        author_markers: list[str] | None = None,
        earlier: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._review_policy = policy
        self._review_exclusion = exclusion
        self.REPO = "acme/widgets"
        self.trailers = trailers
        self.earlier = earlier or []
        self.author_markers = author_markers or []
        self.posted: list[str] = []
        self.labels: list[tuple[int, str, str]] = []
        self.repairs: list[dict[str, object]] = []

    def gh_json(self, args):
        models = [*self.earlier, *self.trailers]
        return [[{"commit": {"message": f"work\n\nModel: {m}"}} for m in models]]

    def repo_fetch(self, path):
        if path != f"commits/{HEAD}":
            return None
        message = "work\n\n" + "\n".join(f"Model: {m}" for m in self.trailers)
        return {"sha": HEAD, "parents": [{"sha": OLDER}], "commit": {"message": message}}

    def pr_json(self, number):
        return {
            "headRefOid": HEAD,
            "state": "OPEN",
            "isDraft": False,
            "labels": [],
            "headRefName": "x",
            "baseRefName": "main",
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
    def make(
        *trailers: str, markers: list[str] | None = None, earlier: list[str] | None = None
    ) -> Ctrl:
        fake = Ctrl(list(trailers), markers, earlier)
        monkeypatch.setattr(fe, "_controller", fake)
        return fake

    return make


def test_review_by_the_head_commit_model_is_refused(ctrl) -> None:
    ctrl("claude-opus-5-5")
    with pytest.raises(ValueError, match="authored the current head"):
        fe.record(pr_number=4, head=HEAD, actor="alice", review=review(), environment=ACTIONS)


def test_review_by_the_recorded_head_author_is_refused(ctrl) -> None:
    marker = policy.agent_model_author_marker(pr=4, head=HEAD, model="claude-opus-5-5", agent="9")
    ctrl("gpt-5.2", markers=[marker])
    with pytest.raises(ValueError, match="authored the current head"):
        fe.record(pr_number=4, head=HEAD, actor="alice", review=review(), environment=ACTIONS)


def test_an_earlier_head_author_may_review(ctrl) -> None:
    older = policy.agent_model_author_marker(pr=4, head=OLDER, model="claude-opus-5-5", agent="9")
    head = policy.agent_model_author_marker(pr=4, head=HEAD, model="gpt-5.2", agent="12")
    fake = ctrl("gpt-5.2", markers=[older, head], earlier=["claude-opus-5-5"])
    result = fe.record(
        pr_number=4,
        head=HEAD,
        actor="alice",
        review=review("CHANGES_REQUESTED"),
        environment=ACTIONS,
    )
    assert result["verdict"] == "repair" and fake.repairs


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
