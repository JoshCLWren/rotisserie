"""External agents race over a trusted shared lease, not candidate labels."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import external, frontier, queue

REPO = "acme/widgets"


class GitHubDouble:
    repo = REPO

    def __init__(
        self,
        *,
        permission: str = "write",
        head: str = "a" * 40,
        priority: str = "ralph-priority:low",
        dependency_open: bool = False,
    ) -> None:
        self.permission = permission
        self.head = head
        self.priority = priority
        self.dependency_open = dependency_open
        self.review_labels: list[str] = []
        self.review_issue = 7

    def whoami(self) -> str:
        return "alice"

    def repo_api(self, route: str) -> Any:
        if route.startswith("collaborators/"):
            return {"permission": self.permission}
        if "/dependencies/blocked_by" in route:
            return [{"state": "open", "number": 99}] if self.dependency_open else []
        if route.startswith("issues/"):
            return {"state": "open", "labels": [{"name": self.priority}]}
        if route.startswith("pulls/"):
            return {
                "state": "open",
                "title": "Implement issue",
                "body": f"Closes #{self.review_issue}",
                "draft": False,
                "labels": [{"name": x} for x in self.review_labels],
                "head": {"sha": self.head, "ref": "work-7"},
            }
        raise AssertionError(route)

    def gh(self, *args: str, **kwargs: Any) -> str:
        assert args[:1] == ("api",)
        assert "issues/88/labels" in str(args[1])
        self.review_labels = ["factory", "factory:review", "factory:unowned"]
        return "{}"


class LedgerDouble:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.holder: str | None = None
        self.calls = 0
        self.code_authors: set[str] = set()

    def external_claim_details(
        self, repo: str, actor: str, client: str, claim_id: str
    ) -> dict[str, object]:
        if self.holder != client:
            raise RuntimeError("claim identity changed")
        return {
            "target": "issue:7",
            "action": "implement",
            "head": None,
            "targets": [7],
            "client": client,
            "actor": actor,
            "claim_id": claim_id,
        }

    def external_code_authors(self, repo: str, numbers: list[int]) -> set[str]:
        assert repo == REPO
        assert numbers
        return self.code_authors

    def reserve_external(
        self, repo: str, actor: str, client: str, numbers: list[int], **kwargs: Any
    ) -> dict[str, object]:
        assert repo == REPO
        assert actor == "alice"
        with self.lock:
            self.calls += 1
            if self.holder:
                return {
                    "status": "conflict",
                    "reason": "existing_or_unreconciled_lease",
                    "retry": "refresh_frontier",
                }
            self.holder = client
            return {
                "status": "reserved",
                "claim_id": "11111111-1111-4111-8111-111111111111",
                "expires_at": "2026-11-22T23:00:00Z",
            }

    def external_lease_action(
        self, repo: str, actor: str, client: str, claim_id: str, **kwargs: Any
    ) -> dict[str, object]:
        with self.lock:
            if client != self.holder:
                return {"status": "conflict", "reason": "stale_or_wrong_client"}
            if kwargs.get("operation") in {"release", "handoff"}:
                self.holder = None
                return {
                    "status": "released",
                    "handoff_recorded": kwargs.get("operation") == "handoff",
                }
            return {"status": "renewed"}


def make_offer(
    *, number: int = 7, action: str = "implement", contributors: list[str] | None = None
) -> frontier.Offer:
    return frontier.Offer(
        rank=1,
        target=f"issue:{number}",
        url=f"https://github.com/{REPO}/issues/{number}",
        title=f"Item {number}",
        action=action,
        instruction="Implement the acceptance criteria",
        reason="controller",
        status="candidate",
        hold_reasons=(),
        lane=3,
        priority=1,
        prerequisites=(),
        blocked_by=(),
        head_sha=None,
        owner="factory:unowned",
        lease=None,
        capacity={"pr_wip": 0, "wip_limit": 5},
        parallel_group=f"isolated:issue:{number}",
        parallel_confidence="unknown",
        provenance={"head_contributors": contributors or [], "producer": None},
    )


def make_frontier(*offers: frontier.Offer) -> frontier.Frontier:
    now = datetime.now(UTC)
    return frontier.Frontier(
        schema_version=1,
        repository=REPO,
        generated_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=60)).isoformat(),
        authoritative_claim_available=True,
        authority_note="",
        offers=offers,
        holds=(),
    )


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> LedgerDouble:
    store = LedgerDouble()
    monkeypatch.setattr(external, "ledger", lambda: cast(Any, store))
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(make_offer()))
    monkeypatch.setattr(external, "_live_offer", lambda *a, **k: (True, ""))
    return store


def test_reserve_returns_real_fenced_claim_and_handoff(setup: LedgerDouble) -> None:
    result = external.reserve_next(
        cast(GitHub, GitHubDouble()),
        client="codebuff-phone",
        environment={},
    )
    assert result["status"] == "reserved"
    handoff = result["handoff"]
    assert isinstance(handoff, dict)
    assert handoff["target"] == "issue:7"
    assert handoff["action"] == "implement"
    assert handoff["renew"].startswith("rotisserie factory renew ")
    assert setup.holder == "codebuff-phone"


def test_two_external_clients_race_one_offer_exactly_one_wins(setup: LedgerDouble) -> None:
    barrier = threading.Barrier(2)

    def attempt(client: str) -> dict[str, object]:
        barrier.wait()
        return external.reserve_next(cast(GitHub, GitHubDouble()), client=client, environment={})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, "chatgpt")
        second = pool.submit(attempt, "codebuff")
        answers = [first.result(), second.result()]
    assert sorted(str(answer["status"]) for answer in answers) == ["conflict", "reserved"]
    assert setup.calls == 2
    loser = next(a for a in answers if a["status"] == "conflict")
    assert loser["retry"] == "refresh_frontier"


def test_revoked_permission_cannot_acquire(setup: LedgerDouble) -> None:
    with pytest.raises(ValueError, match="write access"):
        external.reserve_next(
            cast(GitHub, GitHubDouble(permission="read")),
            client="chatgpt",
            environment={},
        )
    assert setup.calls == 0


def test_reordered_target_is_rejected_without_acquisition(setup: LedgerDouble) -> None:
    result = external.reserve_next(
        cast(GitHub, GitHubDouble()),
        client="chatgpt",
        expected="issue:42",
        environment={},
    )
    assert result["reason"] == "offer_reordered_or_no_longer_highest"
    assert setup.calls == 0


def test_expired_observation_requires_refresh(setup: LedgerDouble) -> None:
    result = external.reserve_next(
        cast(GitHub, GitHubDouble()),
        client="chatgpt",
        environment={},
        advertised_at="2026-11-21T01:00:00+00:00",
    )
    assert result["reason"] == "offer_snapshot_expired"
    assert setup.calls == 0


def test_identity_fences_renewal_and_release(setup: LedgerDouble) -> None:
    external.reserve_next(cast(GitHub, GitHubDouble()), client="chatgpt", environment={})
    token = "11111111-1111-4111-8111-111111111111"
    stale = external.claim_action(
        cast(GitHub, GitHubDouble()),
        client="codebuff",
        claim_id=token,
        operation="release",
        environment={},
    )
    assert stale["status"] == "conflict"
    renewed = external.claim_action(
        cast(GitHub, GitHubDouble()),
        client="chatgpt",
        claim_id=token,
        operation="renew",
        environment={},
    )
    assert renewed["status"] == "renewed"
    released = external.claim_action(
        cast(GitHub, GitHubDouble()),
        client="chatgpt",
        claim_id=token,
        operation="release",
        environment={},
    )
    assert released["status"] == "released"
    assert setup.holder is None


def test_untrusted_github_event_cannot_impersonate_dispatch(setup: LedgerDouble) -> None:
    with pytest.raises(ValueError, match="original authorized"):
        external.reserve_next(
            cast(GitHub, GitHubDouble()),
            client="chatgpt",
            environment={
                "GITHUB_ACTIONS": "true",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_ACTOR": "alice",
                "GITHUB_RUN_ATTEMPT": "1",
            },
        )
    assert setup.calls == 0


def test_handoff_evidence_is_repo_bound(setup: LedgerDouble) -> None:
    with pytest.raises(ValueError, match="same-repository"):
        external.claim_action(
            cast(GitHub, GitHubDouble()),
            client="chatgpt",
            claim_id="11111111-1111-4111-8111-111111111111",
            operation="handoff",
            environment={},
            evidence="https://evil.example/exfiltrate?token=123",
        )


def test_unknown_neon_liveness_holds_all_work(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = make_frontier(make_offer())
    bad = frontier.Frontier(
        schema_version=bad.schema_version,
        repository=bad.repository,
        generated_at=bad.generated_at,
        expires_at=bad.expires_at,
        authoritative_claim_available=False,
        authority_note="Neon observation unavailable",
        offers=bad.offers,
        holds=bad.holds,
    )
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: bad)
    result = external.reserve_next(cast(GitHub, GitHubDouble()), client="chatgpt", environment={})
    assert result["status"] == "held"
    assert "Neon" in str(result["reason"])
    assert setup.calls == 0


def test_changed_pr_head_rejected_before_neon_claim(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    offer = replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="review",
        head_sha="a" * 40,
    )
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(offer))
    monkeypatch.setattr(
        external,
        "_linked_targets",
        lambda g, o: (
            [44] if g.head == "a" * 40 else (_ for _ in ()).throw(ValueError("head changed"))
        ),
    )
    result = external.reserve_next(
        cast(GitHub, GitHubDouble(head="b" * 40)),
        client="codebuff",
        environment={},
    )
    assert result["status"] == "held"
    assert "head changed" in str(result["reason"])
    assert setup.calls == 0


def test_dependency_change_holds_issue_before_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = make_frontier(make_offer())
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: snapshot)
    monkeypatch.setattr(queue, "open_blocker", lambda *a, **k: (True, "New blocker"))
    ok, reason = external._live_offer(cast(GitHub, GitHubDouble()), snapshot.offers[0])
    assert not ok
    assert reason == "New blocker"


def test_review_waits_for_required_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    offered = replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="review",
        head_sha="a" * 40,
    )
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(offered))
    monkeypatch.setattr(external, "_required_checks_outcome", lambda *a, **k: "waiting")
    ok, reason = external._live_offer(cast(GitHub, GitHubDouble()), offered)
    assert not ok
    assert "required CI" in reason


def test_known_external_code_author_cannot_reserve_own_review(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="review",
        head_sha="a" * 40,
    )
    second = replace(make_offer(number=8), rank=2)
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(review, second))
    monkeypatch.setattr(
        external, "_linked_targets", lambda g, o: [44] if o.action == "review" else [8]
    )
    setup.code_authors.add("chatgpt")
    result = external.reserve_next(cast(GitHub, GitHubDouble()), client="chatgpt", environment={})
    assert result["status"] == "reserved"
    handoff = result["handoff"]
    assert isinstance(handoff, dict)
    assert handoff["target"] == "issue:8"


def test_priority_changed_after_neon_cas_releases_our_claim(
    setup: LedgerDouble,
) -> None:
    result = external.reserve_next(
        cast(GitHub, GitHubDouble(priority="ralph-priority:critical")),
        client="chatgpt",
        environment={},
    )
    assert result["status"] == "conflict"
    assert "priority" in str(result["reason"])
    assert setup.calls == 1
    assert setup.holder is None


def test_dependency_opened_after_neon_cas_releases_our_claim(
    setup: LedgerDouble,
) -> None:
    result = external.reserve_next(
        cast(GitHub, GitHubDouble(dependency_open=True)),
        client="codebuff",
        environment={},
    )
    assert result["status"] == "conflict"
    assert "99" in str(result["reason"])
    assert setup.calls == 1
    assert setup.holder is None


def test_fenced_implementation_handoff_enrolls_linked_pr_for_independent_review(
    setup: LedgerDouble,
) -> None:
    github = GitHubDouble()
    reserved = external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    assert reserved["status"] == "reserved"
    delivered = external.claim_action(
        cast(GitHub, github),
        client="chatgpt",
        claim_id="11111111-1111-4111-8111-111111111111",
        operation="handoff",
        environment={},
        evidence=f"https://github.com/{REPO}/pull/88",
    )
    assert delivered["handoff_recorded"] is True
    assert github.review_labels == ["factory", "factory:review", "factory:unowned"]
    assert setup.holder is None


def test_unrelated_handoff_pr_cannot_release_implementation_claim(
    setup: LedgerDouble,
) -> None:
    github = GitHubDouble()
    github.review_issue = 99
    external.reserve_next(cast(GitHub, github), client="codebuff", environment={})
    with pytest.raises(ValueError, match="linked issue"):
        external.claim_action(
            cast(GitHub, github),
            client="codebuff",
            claim_id="11111111-1111-4111-8111-111111111111",
            operation="handoff",
            environment={},
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "codebuff"
    assert github.review_labels == []
