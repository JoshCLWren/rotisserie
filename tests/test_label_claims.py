"""GitHub label claims and the label doctor (issue #191)."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

import pytest

from rotisserie.factory_console.github import GitHub, GitHubNotFoundError
from rotisserie.factory_console.service import external, frontier, labelclaims, labels

REPO = "acme/widgets"
NOW = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)
WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/rotisserie-label-claim.yml"
CALLERS = Path(__file__).resolve().parents[1] / "factory/install/callers"


class FakeGitHub:
    repo = REPO

    def __init__(self, items: dict[int, set[str]] | None = None) -> None:
        self.items: dict[int, set[str]] = items or {}
        self.kinds: dict[int, str] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.writes: list[tuple[str, int, str]] = []
        self.dispatched: list[dict[str, str]] = []
        self.definitions: list[str] = []
        self.events: dict[int, list[dict[str, Any]]] = {}
        self.closed: set[int] = set()
        self.updated: dict[int, str] = {}
        self.lock = threading.Lock()

    def _row(self, number: int) -> dict[str, Any]:
        row: dict[str, Any] = {
            "number": number,
            "state": "closed" if number in self.closed else "open",
            "labels": [{"name": n} for n in sorted(self.items[number])],
        }
        if number in self.updated:
            row["updated_at"] = self.updated[number]
        if self.kinds.get(number) == "pr":
            row["pull_request"] = {}
        return row

    def repo_api(self, route: str) -> Any:
        if route.startswith("issues/"):
            return self._row(int(route.split("/")[1]))
        raise AssertionError(route)

    def workflow_run(self, workflow: str, inputs: dict[str, str], *, ref: str = "main") -> str:
        self.dispatched.append({"workflow": workflow, **inputs})
        return ""

    def gh(self, *args: str, **kwargs: Any) -> str:
        with self.lock:
            return self._gh(*args)

    def _gh(self, *args: str) -> str:
        route = args[3] if args[:3] == ("api", "--paginate", "--slurp") else args[1]
        if args[:3] == ("api", "--paginate", "--slurp"):
            if route.endswith("/labels?per_page=100"):
                return json.dumps([[{"name": n} for n in self.definitions]])
            if "/issues?state=open" in route:
                return json.dumps([[self._row(n) for n in sorted(self.items)]])
            if route.endswith("/events?per_page=100"):
                number = int(route.split("/")[4])
                return json.dumps([self.events.get(number, [])])
            if "/comments?per_page=100" in route:
                number = int(route.split("/")[4])
                return json.dumps([self.comments.get(number, [])])
            raise AssertionError(args)
        parts = route.split("/")
        if parts[-1] == "labels" and "POST" in args:
            number = int(parts[4])
            for value in args:
                if value.startswith("labels[]="):
                    name = value.removeprefix("labels[]=")
                    self.items[number].add(name)
                    self.writes.append(("add", number, name))
            return "{}"
        if "labels" in parts and "DELETE" in args:
            if parts[3] == "labels":
                self.definitions.remove(unquote(parts[4]))
                self.writes.append(("delete-definition", 0, unquote(parts[4])))
                return "{}"
            number = int(parts[4])
            name = unquote(parts[6])
            if name not in self.items[number]:
                raise GitHubNotFoundError("GitHub says that does not exist or you cannot see it.")
            self.items[number].discard(name)
            self.writes.append(("remove", number, name))
            return "{}"
        if route.endswith("/comments") and "POST" in args:
            number = int(parts[4])
            body = args[-1].removeprefix("body=")
            self.comments.setdefault(number, []).append(
                {
                    "id": number * 100 + len(self.comments.get(number, [])),
                    "body": body,
                    "user": {"login": "github-actions[bot]"},
                }
            )
            return "{}"
        if "/issues/comments/" in route and "PATCH" in args:
            ident = int(parts[-1])
            for rows in self.comments.values():
                for row in rows:
                    if row["id"] == ident:
                        row["body"] = args[-1].removeprefix("body=")
            return "{}"
        raise AssertionError(args)


def offer(target: str, action: str, status: str = "candidate", lease: Any = None) -> Any:
    kind, number = target.split(":")
    return frontier.Offer(
        rank=1,
        target=target,
        url="",
        title="",
        action=action,
        instruction="",
        reason="",
        status=status,
        hold_reasons=() if status == "candidate" else ("held",),
        lane=3,
        priority=2,
        prerequisites=(),
        blocked_by=(),
        head_sha="a" * 40 if kind == "pr" else None,
        owner=None,
        lease=lease,
        capacity={"pr_wip": 0, "wip_limit": 5, "review_backlog": 0, "review_limit": 5},
        parallel_group="",
        parallel_confidence="",
        provenance={},
    )


def snapshot(*offers: Any, authoritative: bool = True) -> frontier.Frontier:
    return frontier.Frontier(
        1,
        REPO,
        NOW.isoformat(),
        NOW.isoformat(),
        authoritative,
        "" if authoritative else "Neon down",
        tuple(offers),
        (),
    )


@pytest.fixture(autouse=True)
def no_neon_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frontier, "_active_leases", lambda repo: ({}, ""))


ACTIONS_ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_EVENT_NAME": "issues",
    "GITHUB_REPOSITORY": REPO,
    "GITHUB_ACTOR": "alice",
    "GITHUB_TRIGGERING_ACTOR": "alice",
    "GITHUB_RUN_ATTEMPT": "1",
}


def event(
    label: str, action: str, number: int = 7, sender: str = "alice", kind: str = "User"
) -> dict[str, Any]:
    return {
        "action": action,
        "label": {"name": label},
        "issue": {"number": number, "labels": []},
        "sender": {"login": sender, "type": kind},
    }


# Doctor -----------------------------------------------------------------------------


def test_doctor_dry_run_writes_nothing_and_shows_exact_changes() -> None:
    github = FakeGitHub(
        {7: {"factory", "factory:unowned"}, 9: {"factory", "factory:review", "factory:ci", "bug"}}
    )
    github.kinds[9] = "pr"
    report, plans = labels.audit(
        cast(GitHub, github), now=NOW, snapshot=snapshot(offer("issue:7", "implement"))
    )
    assert github.writes == []
    changes = {row["target"]: row for row in report.items}
    assert changes["issue:7"]["add"] == ["factory:available"]
    assert changes["pr:9"]["remove"] == ["factory:ci"]
    assert "bug" in cast(list[str], changes["pr:9"]["after"])


def test_doctor_repair_converges_and_second_pass_is_a_no_op() -> None:
    github = FakeGitHub(
        {
            7: {"factory", "factory:unowned", '"factory:review"'},
            9: {"factory", "factory:review", "factory:ci", "factory:available"},
        }
    )
    github.kinds[9] = "pr"
    snap = snapshot(offer("issue:7", "implement"), offer("pr:9", "review", status="held"))
    first = labels.repair(cast(GitHub, github), now=NOW, snapshot=snap)
    assert first.applied
    assert github.items[7] == {"factory", "factory:unowned", "factory:available"}
    assert github.items[9] == {"factory", "factory:review"}
    writes = len(github.writes)
    second = labels.repair(cast(GitHub, github), now=NOW, snapshot=snap)
    assert second.items == [] and len(github.writes) == writes


def test_doctor_withholds_availability_when_neon_is_unknown() -> None:
    github = FakeGitHub(
        {
            7: {"factory", "factory:unowned", "factory:available"},
            8: {"factory", "factory:unowned", "factory:claimed"},
        }
    )
    github.events[8] = [
        {
            "event": "labeled",
            "label": {"name": "factory:claimed"},
            "created_at": "2026-11-01T00:00:00Z",
        }
    ]
    snap = snapshot(
        offer("issue:7", "implement"), offer("issue:8", "implement"), authoritative=False
    )
    labels.repair(cast(GitHub, github), now=NOW, snapshot=snap)
    assert github.items[7] == {"factory", "factory:unowned"}
    assert github.items[8] == {"factory", "factory:unowned", "factory:claimed"}


def test_doctor_ages_a_claim_label_by_the_last_update_when_events_are_missing() -> None:
    # GitHub stops listing events on a busy item, so the label add can be absent.
    github = FakeGitHub(
        {
            7: {"factory", "factory:unowned", "factory:claimed"},
            8: {"factory", "factory:unowned", "factory:claimed"},
        }
    )
    github.updated = {7: "2026-11-01T00:00:00Z", 8: NOW.isoformat()}
    snap = snapshot(offer("issue:7", "implement"), offer("issue:8", "implement"))
    report = labels.repair(cast(GitHub, github), now=NOW, snapshot=snap)
    assert "factory:claimed" not in github.items[7]
    assert "factory:claimed" in github.items[8]
    assert {"target": "issue:8", "holds": ["claim request not answered yet"]} in report.holds


def test_doctor_skips_items_changed_since_audit() -> None:
    github = FakeGitHub({7: {"factory", "factory:unowned"}})
    report, plans = labels.audit(
        cast(GitHub, github), now=NOW, snapshot=snapshot(offer("issue:7", "implement"))
    )
    github.items[7].add("factory:3")
    stale = labels.repair(
        cast(GitHub, github),
        now=NOW,
        snapshot=snapshot(offer("issue:7", "implement")),
        items=[{"number": 7, "labels": [{"name": "factory"}, {"name": "factory:unowned"}]}],
    )
    assert stale.skipped == [{"target": "issue:7", "reason": "labels changed since audit"}]
    assert "factory:available" not in github.items[7]


def test_legacy_definitions_are_deleted_only_with_the_flag() -> None:
    github = FakeGitHub({})
    github.definitions = ["bug", "factory:in-review", '"factory:ready"', "ralph-status:done"]
    labels.repair(cast(GitHub, github), now=NOW, snapshot=snapshot())
    assert len(github.definitions) == 4
    report = labels.repair(cast(GitHub, github), now=NOW, snapshot=snapshot(), delete_legacy=True)
    assert report.deleted_definitions == ['"factory:ready"', "factory:in-review"]
    assert github.definitions == ["bug", "ralph-status:done"]


# Claim requests ---------------------------------------------------------------------


class Neon:
    """One fenced lease per item: the first reservation wins, as the ledger CAS does."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.claims: dict[int, dict[str, object]] = {}

    def reserve(
        self, github: Any, *, client: str, environment: dict[str, str], target: str, **_: Any
    ) -> dict[str, object]:
        number = int(target.split(":")[1])
        with self.lock:
            if number in self.claims:
                return {"status": "conflict", "reason": "existing_or_unreconciled_lease"}
            claim: dict[str, object] = {
                "claim_id": f"uuid-{environment['GITHUB_ACTOR']}",
                "actor": environment["GITHUB_ACTOR"],
                "client": client,
                "target": target,
                "action": "implement",
                "head": None,
            }
            self.claims[number] = claim
        return {
            "status": "reserved",
            **claim,
            "expires_at": "soon",
            "handoff": {"action": "implement", "evidence_required": "a PR"},
        }

    def holder(self, github: Any, number: int) -> dict[str, object] | None:
        return self.claims.get(number)

    def action(
        self,
        github: Any,
        *,
        client: str,
        claim_id: str,
        operation: str,
        environment: dict[str, str],
        evidence: str = "",
    ) -> dict[str, object]:
        for number, claim in list(self.claims.items()):
            if claim["claim_id"] == claim_id and claim["actor"] == environment["GITHUB_ACTOR"]:
                if operation in ("release", "handoff"):
                    del self.claims[number]
                    return {"status": "released"}
                return {"status": "renewed"}
        return {"status": "conflict", "reason": "stale_or_wrong_client"}


@pytest.fixture
def neon(monkeypatch: pytest.MonkeyPatch) -> Neon:
    store = Neon()
    monkeypatch.setattr(external, "reserve_target", store.reserve)
    monkeypatch.setattr(external, "holder", store.holder)
    monkeypatch.setattr(external, "claim_action", store.action)
    return store


def test_accepted_claim_keeps_label_and_writes_one_status_comment(neon: Neon) -> None:
    github = FakeGitHub({7: {"factory", "factory:unowned", "factory:available", "factory:claimed"}})
    result = labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "labeled"), dict(ACTIONS_ENV)
    )
    assert result["status"] == "reserved"
    assert github.items[7] == {"factory", "factory:unowned", "factory:claimed", "factory:override"}
    assert len(github.comments[7]) == 1
    assert "Accepted: @alice" in github.comments[7][0]["body"]


def test_two_agents_racing_get_exactly_one_lease(neon: Neon) -> None:
    github = FakeGitHub({7: {"factory", "factory:unowned", "factory:available", "factory:claimed"}})

    def claim(actor: str) -> dict[str, object]:
        env = {**ACTIONS_ENV, "GITHUB_ACTOR": actor, "GITHUB_TRIGGERING_ACTOR": actor}
        return labelclaims.handle(
            cast(GitHub, github), "issues", event("factory:claimed", "labeled", sender=actor), env
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["alice", "bob"]))
    assert sorted(str(r["status"]) for r in results) == ["conflict", "reserved"]
    assert len(neon.claims) == 1
    winner = neon.claims[7]["actor"]
    assert "factory:claimed" in github.items[7]
    assert len(github.comments[7]) == 1
    assert f"Held by @{winner}" in github.comments[7][0]["body"] or (
        f"Accepted: @{winner}" in github.comments[7][0]["body"]
    )


def test_rejected_claim_removes_label_and_says_why(neon: Neon) -> None:
    neon.claims[7] = {
        "claim_id": "w",
        "actor": "worker",
        "client": "codebuff",
        "target": "issue:7",
        "action": "implement",
    }
    github = FakeGitHub({7: {"factory", "factory:unowned", "factory:claimed"}})
    labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "labeled"), dict(ACTIONS_ENV)
    )
    assert github.items[7] == {"factory", "factory:unowned", "factory:claim-rejected"}
    assert "existing_or_unreconciled_lease" in github.comments[7][0]["body"]


def test_another_agent_removing_the_label_cannot_steal_or_end_the_claim(neon: Neon) -> None:
    github = FakeGitHub({7: {"factory", "factory:unowned", "factory:claimed"}})
    labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "labeled"), dict(ACTIONS_ENV)
    )
    github.items[7].discard("factory:claimed")
    env = {**ACTIONS_ENV, "GITHUB_ACTOR": "mallory", "GITHUB_TRIGGERING_ACTOR": "mallory"}
    result = labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "unlabeled", sender="mallory"), env
    )
    assert result["status"] == "restored"
    assert "factory:claimed" in github.items[7]
    assert neon.claims[7]["actor"] == "alice"


def test_claimant_removing_the_label_without_evidence_releases(
    neon: Neon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(labelclaims, "_evidence", lambda github, claim: "")
    github = FakeGitHub({7: {"factory", "factory:unowned", "factory:claimed"}})
    labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "labeled"), dict(ACTIONS_ENV)
    )
    github.items[7].discard("factory:claimed")
    result = labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "unlabeled"), dict(ACTIONS_ENV)
    )
    assert result["operation"] == "release"
    assert neon.claims == {}
    assert github.dispatched == [
        {"workflow": labelclaims.DISPATCH, "target": "issue:7", "action": "advance"}
    ]


def test_finished_claim_with_evidence_hands_off_and_wakes_the_next_stage(
    neon: Neon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        labelclaims, "_evidence", lambda github, claim: f"https://github.com/{REPO}/pull/12"
    )
    github = FakeGitHub({7: {"factory", "factory:unowned", "factory:claimed"}, 12: {"factory"}})
    labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "labeled"), dict(ACTIONS_ENV)
    )
    result = labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "unlabeled"), dict(ACTIONS_ENV)
    )
    assert result["operation"] == "handoff"
    assert "factory:claimed" not in github.items[7] and "factory:override" in github.items[12]
    assert github.dispatched[-1] == {
        "workflow": labelclaims.DISPATCH,
        "target": "pr:12",
        "action": "advance",
    }


def test_finish_clears_the_label_on_every_item_the_lease_covered(
    neon: Neon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        labelclaims, "_evidence", lambda github, claim: f"https://github.com/{REPO}/pull/12"
    )
    github = FakeGitHub(
        {12: {"factory", "factory:claimed"}, 9: {"factory", "factory:review", "factory:claimed"}}
    )
    github.kinds[12] = "pr"
    labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "labeled", 12), dict(ACTIONS_ENV)
    )
    neon.claims[12]["targets"] = [12, 9]
    github.items[12].discard("factory:claimed")
    result = labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "unlabeled", 12), dict(ACTIONS_ENV)
    )
    assert result["operation"] == "handoff"
    assert "factory:claimed" not in github.items[9]


def test_bot_and_mismatched_senders_are_ignored(neon: Neon) -> None:
    github = FakeGitHub({7: {"factory:claimed"}})
    bot = labelclaims.handle(
        cast(GitHub, github),
        "issues",
        event("factory:claimed", "labeled", sender="github-actions[bot]", kind="Bot"),
        dict(ACTIONS_ENV),
    )
    spoof = labelclaims.handle(
        cast(GitHub, github),
        "issues",
        event("factory:claimed", "labeled", sender="bob"),
        dict(ACTIONS_ENV),
    )
    assert bot["status"] == spoof["status"] == "ignored"
    assert github.writes == [] and neon.claims == {}


def test_claimant_comment_renews(neon: Neon) -> None:
    github = FakeGitHub({7: {"factory:claimed"}})
    labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "labeled"), dict(ACTIONS_ENV)
    )
    payload = {
        "action": "created",
        "issue": {"number": 7},
        "sender": {"login": "alice", "type": "User"},
    }
    result = labelclaims.handle(
        cast(GitHub, github),
        "issue_comment",
        payload,
        {**ACTIONS_ENV, "GITHUB_EVENT_NAME": "issue_comment"},
    )
    assert result["status"] == "renewed"


def test_label_events_pass_the_principal_fence_only_from_trusted_events() -> None:
    assert set(external.LABEL_EVENTS) == {"issues", "pull_request_target", "issue_comment"}
    assert "pull_request_review" not in external.LABEL_EVENTS


# Workflow ---------------------------------------------------------------------------


def test_label_claim_workflow_never_runs_pull_request_code() -> None:
    text = (CALLERS / WORKFLOW.name).read_text() + WORKFLOW.read_text()
    assert "pull_request_target:" in text and "issues:" in text
    assert "\n  pull_request:" not in text and "pull_request_review:" not in text
    assert "ref: main" in text and "persist-credentials: false" in text
    assert "github.event.pull_request.head.repo.full_name == github.repository" in text
    assert "github.event.sender.type != 'Bot'" in text
    assert "GITHUB_RUN_ATTEMPT" in text and "github.triggering_actor" in text
    assert "contents: write" not in text
    assert "factory label-event --event-path" in text
    assert "factory labels --repair" in text


def test_issue_wakeup_ignores_claim_labels() -> None:
    wakeup = WORKFLOW.with_name("factory-issue-wakeup.yml").read_text()
    assert "factory:claimed" in wakeup and "github.event.label.name" in wakeup


def test_doctor_does_not_offer_a_pr_whose_checks_are_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(external, "live_now", lambda *a: (False, "required CI is still running"))
    github = FakeGitHub({9: {"factory", "factory:unowned", "factory:ci", "factory:available"}})
    github.kinds[9] = "pr"
    labels.repair(cast(GitHub, github), now=NOW, snapshot=snapshot(offer("pr:9", "ci-repair")))
    assert github.items[9] == {"factory", "factory:unowned", "factory:ci"}
    monkeypatch.setattr(external, "live_now", lambda *a: (True, ""))
    labels.repair(cast(GitHub, github), now=NOW, snapshot=snapshot(offer("pr:9", "ci-repair")))
    assert "factory:available" in github.items[9]


def test_doctor_never_labels_a_manual_only_item() -> None:
    github = FakeGitHub({7: {"factory", "factory:unowned", '"factory:ready"'}})
    rows = [
        {
            "number": 7,
            "body": "<!-- factory-execution:manual-only -->",
            "labels": [{"name": n} for n in sorted(github.items[7])],
        }
    ]
    report = labels.repair(
        cast(GitHub, github), now=NOW, snapshot=snapshot(offer("issue:7", "implement")), items=rows
    )
    assert github.writes == []
    assert report.holds == [{"target": "issue:7", "holds": ["manual-only"]}]


# Outside review hand-off through the real external claim branch ---------------------


class ReviewGitHub:
    repo = REPO

    def __init__(self, reviews: list[dict[str, Any]]) -> None:
        self.reviews = reviews
        self.head = "a" * 40

    def repo_api(self, route: str) -> Any:
        if route.startswith("pulls/"):
            return {
                "state": "open",
                "draft": False,
                "title": "Fix",
                "body": "Closes #7",
                "labels": [{"name": n} for n in ("factory", "factory:review", "factory:unowned")],
                "head": {"sha": self.head, "ref": "work-7"},
            }
        if route.startswith("issues/"):
            return {"state": "open", "labels": []}
        raise AssertionError(route)

    def gh(self, *args: str, **kwargs: Any) -> str:
        if args[:3] == ("api", "--paginate", "--slurp") and args[3].endswith("/reviews"):
            return json.dumps([self.reviews])
        raise AssertionError(args)


def _recorder(tmp_path: Path) -> Path:
    scripts = tmp_path / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "factory_external_review.py").write_text(
        "import json, pathlib\n"
        "def record(**kwargs):\n"
        "    pathlib.Path(__file__).with_name('called.json').write_text(json.dumps(\n"
        "        {k: v for k, v in kwargs.items() if k != 'environment'}))\n"
    )
    return scripts / "called.json"


def test_review_hand_off_records_the_claimants_review_on_the_claimed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = _recorder(tmp_path)
    monkeypatch.chdir(tmp_path)
    head = "a" * 40
    reviews = [
        {"id": 1, "user": {"login": "bob"}, "commit_id": head, "state": "APPROVED"},
        {"id": 2, "user": {"login": "alice"}, "commit_id": "c" * 40, "state": "APPROVED"},
        {"id": 3, "user": {"login": "alice"}, "commit_id": head, "state": "COMMENTED"},
        {"id": 4, "user": {"login": "alice"}, "commit_id": head, "state": "APPROVED"},
        {"id": 5, "user": {"login": "alice"}, "commit_id": "e" * 40, "state": "APPROVED"},
        {"id": 6, "user": {"login": "bob"}, "commit_id": head, "state": "CHANGES_REQUESTED"},
    ]
    claim = {
        "target": "pr:12",
        "action": "review",
        "head": head,
        "targets": [12, 7],
        "client": "label",
        "actor": "alice",
        "claim_id": "u",
    }
    external._verify_and_project_handoff(
        cast(GitHub, ReviewGitHub(reviews)),
        claim,
        f"https://github.com/{REPO}/pull/12",
        {"GITHUB_ACTIONS": "true"},
    )
    recorded = json.loads(called.read_text())
    assert recorded["review"]["id"] == 4
    assert recorded["pr_number"] == 12 and recorded["actor"] == "alice"


def test_review_hand_off_refuses_without_a_decisive_review_or_outside_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = _recorder(tmp_path)
    monkeypatch.chdir(tmp_path)
    claim = {
        "target": "pr:12",
        "action": "review",
        "head": "a" * 40,
        "targets": [12, 7],
        "client": "label",
        "actor": "alice",
        "claim_id": "u",
    }
    url = f"https://github.com/{REPO}/pull/12"
    with pytest.raises(ValueError, match="no Approve or Request changes"):
        external._verify_and_project_handoff(
            cast(GitHub, ReviewGitHub([])), claim, url, {"GITHUB_ACTIONS": "true"}
        )
    approved = [{"id": 4, "user": {"login": "alice"}, "commit_id": "a" * 40, "state": "APPROVED"}]
    with pytest.raises(ValueError, match="run in the GitHub bridge"):
        external._verify_and_project_handoff(cast(GitHub, ReviewGitHub(approved)), claim, url, {})
    moved = ReviewGitHub(approved)
    moved.head = "d" * 40
    with pytest.raises(ValueError, match="PR moved since the review claim"):
        external._verify_and_project_handoff(
            cast(GitHub, moved), claim, url, {"GITHUB_ACTIONS": "true"}
        )
    assert not called.exists()


def test_claim_on_closed_work_is_dropped_without_a_rejection_label(neon: Neon) -> None:
    github = FakeGitHub({7: {"factory", "factory:claimed"}})
    github.closed.add(7)
    result = labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "labeled"), dict(ACTIONS_ENV)
    )
    assert result["reason"] == "closed"
    assert github.items[7] == {"factory"}
    assert neon.claims == {}
    assert "Reopen issue:7, then add `factory:claimed` again." in github.comments[7][0]["body"]


def test_accepted_claim_on_an_item_without_availability_still_answers(neon: Neon) -> None:
    """Live bug: removing an absent label raised and left an unanswered Neon claim."""
    github = FakeGitHub({7: {"factory", "factory:unowned", "factory:claimed"}})
    result = labelclaims.handle(
        cast(GitHub, github), "issues", event("factory:claimed", "labeled"), dict(ACTIONS_ENV)
    )
    assert result["status"] == "reserved"
    assert "Accepted: @alice" in github.comments[7][0]["body"]
