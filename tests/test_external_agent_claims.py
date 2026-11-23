"""External agents race over a trusted shared lease, not candidate labels."""

from __future__ import annotations

import json
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
        self.closed: set[int] = set()
        self.label_writes = 0
        self.full_name = REPO
        self.message = "Fix it\n\nModel: gpt-5.2"
        self.comments: list[str] = []
        self.pr_body = ""
        self.branch = "work-7"
        self.issue_labels: list[str] = []
        self.pr_author = "acme"

    def whoami(self) -> str:
        return "alice"

    def repo_api(self, route: str) -> Any:
        if route.startswith("collaborators/"):
            return {"permission": self.permission}
        if "/dependencies/blocked_by" in route:
            return [{"state": "open", "number": 99}] if self.dependency_open else []
        if route.startswith("issues/"):
            number = int(route.split("/")[1])
            state = "closed" if number in self.closed else "open"
            names = [self.priority, *self.issue_labels]
            return {"state": state, "labels": [{"name": n} for n in names]}
        if route.startswith("commits/"):
            return {"commit": {"message": self.message}}
        if route.startswith("pulls/"):
            return {
                "state": "open",
                "title": "Implement issue",
                "body": self.pr_body or f"Closes #{self.review_issue}",
                "user": {"login": self.pr_author},
                "draft": False,
                "labels": [{"name": x} for x in self.review_labels],
                "head": {"sha": self.head, "ref": self.branch},
            }
        raise AssertionError(route)

    def gh(self, *args: str, **kwargs: Any) -> str:
        if args == ("api", f"repos/{REPO}"):
            return json.dumps({"full_name": self.full_name})
        if args[:2] == ("issue", "comment"):
            self.comments.append(args[-1])
            return ""
        assert args[:1] == ("api",)
        assert args[1].endswith("/labels")
        assert args[2:4] == ("--method", "PUT")
        self.label_writes += 1
        self.review_labels = list(json.loads(kwargs["stdin"])["labels"])
        return "{}"


BRIDGE = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REPOSITORY": REPO,
    "GITHUB_ACTOR": "alice",
    "GITHUB_TRIGGERING_ACTOR": "alice",
    "GITHUB_RUN_ATTEMPT": "1",
}


class LedgerDouble:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.holder: str | None = None
        self.calls = 0
        self.claim: dict[str, object] = {
            "target": "issue:7",
            "action": "implement",
            "head": None,
            "targets": [7],
        }
        self.rows: list[dict[str, object]] = []
        self.revoked: list[tuple[str, str]] = []
        self.requests: list[dict[str, Any]] = []

    def list_leases(self, repo: str) -> list[dict[str, object]]:
        assert repo == REPO
        return list(self.rows)

    def revoke_external(
        self, repo: str, operator: str, claim_id: str, *, reason: str
    ) -> dict[str, object]:
        self.revoked.append((claim_id, reason))
        return {"status": "revoked", "claim_id": claim_id}

    def reserve_external(
        self, repo: str, actor: str, client: str, numbers: list[int], **kwargs: Any
    ) -> dict[str, object]:
        assert repo == REPO
        assert actor == "alice"
        with self.lock:
            self.calls += 1
            self.requests.append(kwargs)
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
            verify = kwargs.get("verify")
            if verify is not None:
                verify({**self.claim, "client": client, "actor": actor, "claim_id": claim_id})
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
        action="review-findings",
        head_sha="a" * 40,
    )
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(offer))
    result = external.reserve_next(
        cast(GitHub, GitHubDouble(head="b" * 40)),
        client="codebuff",
        environment={},
    )
    assert result["status"] == "conflict"
    assert "head changed" in str(result["reason"])
    assert setup.calls == 0


def test_dependency_change_holds_issue_before_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = make_frontier(make_offer())
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: snapshot)
    monkeypatch.setattr(queue, "open_blocker", lambda *a, **k: (True, "New blocker"))
    ok, reason = external._live_offer(cast(GitHub, GitHubDouble()), snapshot.offers[0])
    assert not ok
    assert reason == "New blocker"


def test_repair_waits_for_required_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    offered = replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="review-findings",
        head_sha="a" * 40,
    )
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(offered))
    monkeypatch.setattr(external, "_required_checks_outcome", lambda *a, **k: "waiting")
    ok, reason = external._live_offer(cast(GitHub, GitHubDouble()), offered)
    assert not ok
    assert "required CI" in reason


def test_review_offers_are_never_reserved_by_outside_agents(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge gate cannot count an outside review, so the next offer wins."""
    review = replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="review",
        head_sha="a" * 40,
    )
    second = replace(make_offer(number=8), rank=2)
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(review, second))
    result = external.reserve_next(cast(GitHub, GitHubDouble()), client="chatgpt", environment={})
    assert result["status"] == "reserved"
    handoff = result["handoff"]
    assert isinstance(handoff, dict)
    assert handoff["target"] == "issue:8"
    with pytest.raises(ValueError, match="unsupported agent abilities"):
        external.reserve_next(
            cast(GitHub, GitHubDouble()),
            client="chatgpt",
            environment={},
            abilities=frozenset({"review"}),
        )


def test_capability_filter_picks_highest_offer_the_agent_can_do(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    repair = replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="ci-repair",
        head_sha="a" * 40,
    )
    second = replace(make_offer(number=8), rank=2)
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(repair, second))
    result = external.reserve_next(
        cast(GitHub, GitHubDouble()),
        client="docs-bot",
        environment={},
        abilities=frozenset({"implement"}),
    )
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
        environment=BRIDGE,
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
    with pytest.raises(ValueError, match="claimed issue"):
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


class RevokeLedger:
    def __init__(self, *, live: bool = True, claim: dict[str, object] | None = None) -> None:
        self.live = live
        self.claim = claim
        self.revoked: list[tuple[str, str, str, str]] = []

    def enabled(self) -> bool:
        return self.live

    def external_holder(self, repo: str, number: int) -> dict[str, object] | None:
        assert repo == REPO
        return self.claim

    def revoke_external(
        self, repo: str, operator: str, claim_id: str, *, reason: str
    ) -> dict[str, object]:
        self.revoked.append((repo, operator, claim_id, reason))
        return {"status": "revoked", "claim_id": claim_id}


CLAIM = "11111111-1111-4111-8111-111111111111"


def test_revoke_binds_the_verified_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    store = RevokeLedger()
    monkeypatch.setattr(external, "ledger", lambda: cast(Any, store))
    result = external.revoke(
        cast(GitHub, GitHubDouble()), claim_id=CLAIM, reason="stuck phone", environment={}
    )
    assert result["status"] == "revoked"
    assert store.revoked == [(REPO, "alice", CLAIM, "stuck phone")]


def test_revoke_needs_write_access(monkeypatch: pytest.MonkeyPatch) -> None:
    store = RevokeLedger()
    monkeypatch.setattr(external, "ledger", lambda: cast(Any, store))
    with pytest.raises(ValueError, match="write access"):
        external.revoke(
            cast(GitHub, GitHubDouble(permission="read")),
            claim_id=CLAIM,
            reason="x",
            environment={},
        )
    assert store.revoked == []


def test_revoke_rejects_malformed_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(external, "ledger", lambda: cast(Any, RevokeLedger()))
    with pytest.raises(ValueError, match="claim"):
        external.revoke(cast(GitHub, GitHubDouble()), claim_id="x", reason="y", environment={})


def test_holder_without_neon_says_it_cannot_see(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(external, "ledger", lambda: cast(Any, RevokeLedger(live=False)))
    with pytest.raises(external.ClaimStoreUnavailable):
        external.holder(cast(GitHub, GitHubDouble()), 7)


def test_holder_when_ledger_cannot_load(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise RuntimeError("Neon is not configured here")

    monkeypatch.setattr(external, "ledger", broken)
    with pytest.raises(external.ClaimStoreUnavailable, match="not configured"):
        external.holder(cast(GitHub, GitHubDouble()), 7)


def test_holder_returns_the_external_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    claim: dict[str, object] = {"claim_id": CLAIM, "client": "codebuff-phone"}
    monkeypatch.setattr(external, "ledger", lambda: cast(Any, RevokeLedger(claim=claim)))
    assert external.holder(cast(GitHub, GitHubDouble()), 7) == claim


TOKEN = "11111111-1111-4111-8111-111111111111"


def _repair_claim(setup: LedgerDouble, action: str = "review-findings") -> None:
    setup.holder = "codebuff"
    setup.claim = {"target": "pr:88", "action": action, "head": "a" * 40, "targets": [88]}


@pytest.mark.parametrize(
    ("action", "stage"),
    [
        ("review-findings", "factory:changes-requested"),
        ("ci-repair", "factory:ci"),
        ("merge-conflict", "factory:review"),
    ],
)
def test_repair_handoff_returns_pr_to_review_like_a_worker(
    setup: LedgerDouble, action: str, stage: str
) -> None:
    _repair_claim(setup, action)
    github = GitHubDouble(head="b" * 40)
    github.review_labels = ["factory", stage, "factory:unowned", "area:api"]
    result = external.claim_action(
        cast(GitHub, github),
        client="codebuff",
        claim_id=TOKEN,
        operation="handoff",
        environment=BRIDGE,
        evidence=f"https://github.com/{REPO}/pull/88",
    )
    assert result["handoff_recorded"] is True
    assert github.comments == [
        f"<!-- comic-pile-agent-model-author-v1:pr-88:head-{'b' * 40}:"
        "model-gpt-5.2:agent-external-codebuff -->"
    ]
    assert sorted(github.review_labels) == [
        "area:api",
        "factory",
        "factory:review",
        "factory:unowned",
    ]
    assert github.label_writes == (0 if stage == "factory:review" else 1)
    assert setup.holder is None


def test_repair_handoff_without_new_commit_keeps_the_claim(setup: LedgerDouble) -> None:
    _repair_claim(setup)
    github = GitHubDouble(head="a" * 40)
    github.review_labels = ["factory", "factory:changes-requested", "factory:unowned"]
    with pytest.raises(ValueError, match="no new commit"):
        external.claim_action(
            cast(GitHub, github),
            client="codebuff",
            claim_id=TOKEN,
            operation="handoff",
            environment={},
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "codebuff"
    assert github.label_writes == 0


def test_repair_handoff_cannot_point_at_another_pr(setup: LedgerDouble) -> None:
    _repair_claim(setup)
    setup.claim["target"] = "pr:87"
    github = GitHubDouble(head="b" * 40)
    with pytest.raises(ValueError, match="different PR"):
        external.claim_action(
            cast(GitHub, github),
            client="codebuff",
            claim_id=TOKEN,
            operation="handoff",
            environment={},
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "codebuff"


@pytest.mark.parametrize("route", ["issues/7", "actions/runs/12345"])
def test_only_the_pull_request_is_handoff_evidence(setup: LedgerDouble, route: str) -> None:
    external.reserve_next(cast(GitHub, GitHubDouble()), client="chatgpt", environment={})
    with pytest.raises(ValueError, match="pull request that carries the work"):
        external.claim_action(
            cast(GitHub, GitHubDouble()),
            client="chatgpt",
            claim_id=TOKEN,
            operation="handoff",
            environment={},
            evidence=f"https://github.com/{REPO}/{route}",
        )
    assert setup.holder == "chatgpt"


def test_failed_label_projection_never_loses_the_claim(setup: LedgerDouble) -> None:
    class BrokenLabels(GitHubDouble):
        def gh(self, *args: str, **kwargs: Any) -> str:
            if args[1].endswith("/labels"):
                raise RuntimeError("GitHub refused the label write")
            return super().gh(*args, **kwargs)

    external.reserve_next(cast(GitHub, BrokenLabels()), client="chatgpt", environment={})
    with pytest.raises(RuntimeError, match="refused"):
        external.claim_action(
            cast(GitHub, BrokenLabels()),
            client="chatgpt",
            claim_id=TOKEN,
            operation="handoff",
            environment=BRIDGE,
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "chatgpt"


def test_reconcile_recovers_only_claims_whose_work_github_closed(setup: LedgerDouble) -> None:
    marker = f"factory-lease-v1:{REPO}:external:"
    other = "22222222-2222-4222-8222-222222222222"
    setup.rows = [
        {"key": marker + TOKEN, "claim_id": TOKEN, "targets": [7, 88], "active": False},
        {"key": marker + other, "claim_id": other, "targets": [9], "active": False},
        {"key": f"factory-lease-v1:{REPO}:target:7", "claim_id": TOKEN, "targets": [7, 88]},
    ]
    github = GitHubDouble()
    github.closed = {7, 88}
    removed = external.reconcile(cast(GitHub, github), actor="alice", store=cast(Any, setup))
    assert [row["claim_id"] for row in removed] == [TOKEN]
    assert setup.revoked == [(TOKEN, "work finished on GitHub: #7, #88 closed")]


def test_reconcile_keeps_every_claim_when_neon_cannot_be_read(setup: LedgerDouble) -> None:
    def broken(repo: str) -> list[dict[str, object]]:
        raise RuntimeError("Neon unavailable")

    setup.list_leases = broken  # type: ignore[method-assign]
    assert (
        external.reconcile(cast(GitHub, GitHubDouble()), actor="alice", store=cast(Any, setup))
        == []
    )
    assert setup.revoked == []


def test_handoff_packet_carries_lease_identity_and_both_bridges(setup: LedgerDouble) -> None:
    result = external.reserve_next(
        cast(GitHub, GitHubDouble()), client="codebuff-phone", environment={}
    )
    handoff = result["handoff"]
    assert isinstance(handoff, dict)
    assert handoff["expires_at"] == "2026-11-22T23:00:00Z"
    assert handoff["claim_id"] == TOKEN
    assert "gh workflow run rotisserie-external-claim.yml" in str(handoff["renew_github"])
    assert "-f operation=handoff" in str(handoff["handoff_github"])
    assert "Closes #N" in str(handoff["evidence_required"])
    assert "Model:" in str(handoff["provenance"])
    assert "not approval" in str(handoff["review_and_merge_gates"])


def test_another_spelling_of_the_repository_is_refused(setup: LedgerDouble) -> None:
    github = GitHubDouble()
    github.full_name = "Acme/Widgets"
    with pytest.raises(ValueError, match="exact name: --repo Acme/Widgets"):
        external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    assert setup.calls == 0


def test_a_top_offer_waiting_on_ci_does_not_hide_lower_work(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="review-findings",
        head_sha="a" * 40,
    )
    second = replace(make_offer(number=8), rank=2)
    snapshot = make_frontier(waiting, second)
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: snapshot)
    monkeypatch.setattr(
        external,
        "_live_offer",
        lambda g, o, latest=None: (
            (False, "required CI is still running or unknown")
            if o.target == "pr:44"
            else (True, "")
        ),
    )
    result = external.reserve_next(cast(GitHub, GitHubDouble()), client="chatgpt", environment={})
    handoff = result["handoff"]
    assert isinstance(handoff, dict)
    assert handoff["target"] == "issue:8"
    # An agent that asked for exactly the waiting offer is told why, not moved.
    refused = external.reserve_next(
        cast(GitHub, GitHubDouble()), client="codebuff", environment={}, expected="pr:44"
    )
    assert refused["status"] == "conflict"
    assert "CI" in str(refused["reason"])


def test_policy_wip_bypass_reaches_the_ledger(setup: LedgerDouble) -> None:
    github = GitHubDouble()
    github.issue_labels = ["main-breakage"]
    external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    assert setup.requests[-1]["wip_exempt"] is True
    setup.holder = None
    external.reserve_next(cast(GitHub, GitHubDouble()), client="chatgpt", environment={})
    assert setup.requests[-1]["wip_exempt"] is False


def test_implementation_pr_must_close_exactly_the_claimed_issue(setup: LedgerDouble) -> None:
    github = GitHubDouble()
    github.branch = "factory/3-7-fix"
    github.pr_body = "Closes #99"
    external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    with pytest.raises(ValueError, match="Closes #N"):
        external.claim_action(
            cast(GitHub, github),
            client="chatgpt",
            claim_id=TOKEN,
            operation="handoff",
            environment={},
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "chatgpt"
    assert github.label_writes == 0


@pytest.mark.parametrize("message", ["No trailer", "x\n\nModel: a\nModel: b"])
def test_handoff_needs_one_model_trailer(setup: LedgerDouble, message: str) -> None:
    github = GitHubDouble()
    github.message = message
    external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    with pytest.raises(ValueError, match="exactly one 'Model:"):
        external.claim_action(
            cast(GitHub, github),
            client="chatgpt",
            claim_id=TOKEN,
            operation="handoff",
            environment={},
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "chatgpt"


def test_repair_handoff_outside_the_bridge_is_refused_and_keeps_the_claim(
    setup: LedgerDouble,
) -> None:
    _repair_claim(setup)
    github = GitHubDouble(head="b" * 40)
    github.review_labels = ["factory", "factory:changes-requested", "factory:unowned"]
    with pytest.raises(ValueError, match="GitHub bridge"):
        external.claim_action(
            cast(GitHub, github),
            client="codebuff",
            claim_id=TOKEN,
            operation="handoff",
            environment={},
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "codebuff"
    assert github.comments == []
    assert github.label_writes == 0


def test_required_check_states_match_the_dispatcher() -> None:
    class Checks(GitHubDouble):
        def __init__(self, states: list[str]) -> None:
            super().__init__()
            self.states = states

        def gh(self, *args: str, **kwargs: Any) -> str:
            return json.dumps([{"state": state} for state in self.states])

    outcome = external._required_checks_outcome
    assert outcome(cast(GitHub, Checks(["SUCCESS", "ERROR"])), 1) == "failed"
    assert outcome(cast(GitHub, Checks(["STALE"])), 1) == "failed"
    assert outcome(cast(GitHub, Checks(["ACTION_REQUIRED"])), 1) == "waiting"
    assert outcome(cast(GitHub, Checks(["SUCCESS", "SKIPPED"])), 1) == "passed"


def test_issue_opted_out_after_the_snapshot_releases_our_claim(setup: LedgerDouble) -> None:
    github = GitHubDouble()
    github.issue_labels = ["factory:blocked"]
    result = external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    assert result["status"] == "conflict"
    assert "no longer eligible" in str(result["reason"])
    assert setup.holder is None


def test_title_linked_repair_reserves_its_issue_too(setup: LedgerDouble) -> None:
    class TitleLinked(GitHubDouble):
        def repo_api(self, route: str) -> Any:
            row = super().repo_api(route)
            if route.startswith("pulls/"):
                row = {**row, "title": "Fix #7 crash", "body": "No closing words"}
            return row

    offer = replace(make_offer(), target="pr:44", action="review-findings", head_sha="a" * 40)
    assert external._linked_targets(cast(GitHub, TitleLinked()), offer) == [7, 44]


def test_handoff_names_the_evidence_pr_for_the_ledger_fence(setup: LedgerDouble) -> None:
    seen: list[object] = []
    original = setup.external_lease_action

    def capture(*args: Any, **kwargs: Any) -> dict[str, object]:
        seen.append(kwargs.get("evidence_target"))
        return original(*args, **kwargs)

    setup.external_lease_action = capture  # type: ignore[method-assign]
    external.reserve_next(cast(GitHub, GitHubDouble()), client="chatgpt", environment={})
    external.claim_action(
        cast(GitHub, GitHubDouble()),
        client="chatgpt",
        claim_id=TOKEN,
        operation="handoff",
        environment=BRIDGE,
        evidence=f"https://github.com/{REPO}/pull/88",
    )
    assert seen == [88]


def test_failed_rollback_still_reports_the_claim_we_hold(setup: LedgerDouble) -> None:
    def broken(*args: Any, **kwargs: Any) -> dict[str, object]:
        raise ConnectionResetError("network gone")

    setup.external_lease_action = broken  # type: ignore[method-assign]
    result = external.reserve_next(
        cast(GitHub, GitHubDouble(priority="ralph-priority:critical")),
        client="chatgpt",
        environment={},
    )
    assert result["status"] == "held"
    assert result["claim_id"] == TOKEN
    assert result["retry"] == "check_lease_before_retry"


@pytest.mark.parametrize(
    "body",
    [
        "Closes #7\nCloses #9",
        "Fixes #7 and resolves acme/widgets#9",
        "Closes #7, closes https://github.com/acme/widgets/issues/9",
    ],
)
def test_implementation_pr_may_close_nothing_but_the_claimed_issue(
    setup: LedgerDouble, body: str
) -> None:
    github = GitHubDouble()
    github.pr_body = body
    external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    with pytest.raises(ValueError, match="nothing else"):
        external.claim_action(
            cast(GitHub, github),
            client="chatgpt",
            claim_id=TOKEN,
            operation="handoff",
            environment={},
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "chatgpt"


def test_closing_another_repositorys_issue_is_not_a_conflict() -> None:
    assert external._closed_issues(REPO, "Closes #7. Fixes other/repo#9") == {7}


def test_every_handoff_needs_the_bridge_author_record(
    setup: LedgerDouble,
) -> None:
    github = GitHubDouble()
    github.pr_author = "codebuff[bot]"
    external.reserve_next(cast(GitHub, github), client="codebuff", environment={})
    with pytest.raises(ValueError, match="GitHub bridge"):
        external.claim_action(
            cast(GitHub, github),
            client="codebuff",
            claim_id=TOKEN,
            operation="handoff",
            environment={},
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "codebuff"
    done = external.claim_action(
        cast(GitHub, github),
        client="codebuff",
        claim_id=TOKEN,
        operation="handoff",
        environment=BRIDGE,
        evidence=f"https://github.com/{REPO}/pull/88",
    )
    assert done["handoff_recorded"] is True
    assert github.comments == [
        f"<!-- comic-pile-agent-model-author-v1:pr-88:head-{'a' * 40}:"
        "model-gpt-5.2:agent-external-codebuff -->"
    ]


def test_repair_pr_opted_out_after_the_snapshot_releases_our_claim(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    offer = replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="review-findings",
        head_sha="a" * 40,
    )
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(offer))
    github = GitHubDouble()
    github.review_labels = ["factory", "factory:changes-requested", "factory:unowned"]
    github.pr_body = "<!-- factory-execution:manual-only -->"
    result = external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    assert result["status"] == "conflict"
    assert "no longer eligible" in str(result["reason"])
    assert setup.holder is None


def test_head_that_moves_during_handoff_keeps_the_claim(setup: LedgerDouble) -> None:
    class Moving(GitHubDouble):
        def gh(self, *args: str, **kwargs: Any) -> str:
            result = super().gh(*args, **kwargs)
            if args[1:2] and str(args[1]).endswith("/labels"):
                self.head = "c" * 40  # someone pushed right after verification
            return result

    github = Moving()
    external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    with pytest.raises(ValueError, match="moved during handoff"):
        external.claim_action(
            cast(GitHub, github),
            client="chatgpt",
            claim_id=TOKEN,
            operation="handoff",
            environment=BRIDGE,
            evidence=f"https://github.com/{REPO}/pull/88",
        )
    assert setup.holder == "chatgpt"


def test_pr_link_edited_after_reservation_releases_our_claim(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Relinked(GitHubDouble):
        reads = 0

        def repo_api(self, route: str) -> Any:
            row = super().repo_api(route)
            if route == "pulls/44":
                Relinked.reads += 1
                if Relinked.reads > 1:
                    row = {**row, "body": "Closes #9"}
            return row

    offer = replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="review-findings",
        head_sha="a" * 40,
    )
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(offer))
    github = Relinked()
    github.review_labels = ["factory", "factory:changes-requested", "factory:unowned"]
    result = external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    assert result["status"] == "conflict"
    assert "linked issue changed" in str(result["reason"])
    assert setup.holder is None


def _pr_offer() -> frontier.Offer:
    return replace(
        make_offer(),
        target="pr:44",
        url=f"https://github.com/{REPO}/pull/44",
        action="review-findings",
        head_sha="a" * 40,
    )


def test_repair_of_a_pr_whose_issue_closed_reserves_just_the_pr(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(_pr_offer()))
    monkeypatch.setattr(external, "_required_checks_outcome", lambda *a, **k: "passed")
    github = GitHubDouble()
    github.closed = {7}
    github.review_labels = ["factory", "factory:changes-requested", "factory:unowned"]
    result = external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    assert result["status"] == "reserved", result
    assert setup.holder == "chatgpt"


def test_linked_issue_owner_after_snapshot_releases_our_claim(
    setup: LedgerDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Owned(GitHubDouble):
        reads = 0

        def repo_api(self, route: str) -> Any:
            row = super().repo_api(route)
            if route == "issues/7":
                Owned.reads += 1
                if Owned.reads > 1:
                    row = {**row, "labels": [*row["labels"], {"name": "factory:42"}]}
            return row

    monkeypatch.setattr(frontier, "capture", lambda *a, **k: make_frontier(_pr_offer()))
    github = Owned()
    github.review_labels = ["factory", "factory:changes-requested", "factory:unowned"]
    result = external.reserve_next(cast(GitHub, github), client="chatgpt", environment={})
    assert result["status"] == "conflict"
    assert "linked issue's owner" in str(result["reason"])
    assert setup.holder is None
