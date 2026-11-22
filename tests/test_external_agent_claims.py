"""External agents race over a trusted shared lease, not candidate labels."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import external, frontier

REPO = "acme/widgets"


class GitHubDouble:
    repo = REPO

    def __init__(self, *, permission: str = "write", head: str = "a" * 40) -> None:
        self.permission = permission
        self.head = head

    def whoami(self) -> str:
        return "alice"

    def repo_api(self, route: str) -> Any:
        if route.startswith("collaborators/"):
            return {"permission": self.permission}
        if route.startswith("issues/"):
            return {"state": "open"}
        if route.startswith("pulls/"):
            return {"state": "open", "head": {"sha": self.head, "ref": "work-7"}}
        raise AssertionError(route)


class LedgerDouble:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.holder: str | None = None
        self.calls = 0

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
            if kwargs.get("operation") == "release":
                self.holder = None
                return {"status": "released"}
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
    monkeypatch.setattr(external.frontier, "capture", lambda *a, **k: make_frontier(make_offer()))
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
