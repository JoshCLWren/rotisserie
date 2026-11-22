"""Backlog grooming: full inventory, and plans applied only against fresh live state."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from rotisserie.factory_console.github import JSON, GitHub, GitHubError
from rotisserie.factory_console.service import groom

REPO = "acme/widgets"
OWNED = "factory:1004230852001"


@dataclass
class Fake:
    """Scripted ``gh api`` responses keyed by regex; records every call."""

    routes: dict[str, JSON | Callable[[list[str]], JSON]] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, github: GitHub, args: Sequence[str], *, stdin: str | None = None) -> str:
        argv = list(args)
        self.calls.append(argv)
        if argv[0] != "api":
            return ""
        path = next(
            item
            for index, item in enumerate(argv[1:], start=1)
            if not item.startswith("-") and argv[index - 1] not in ("-f", "--method")
        )
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        for pattern, value in self.routes.items():
            verb, _, regex = pattern.partition(" ") if " " in pattern else ("GET", "", pattern)
            if verb == method and re.search(regex, path):
                found = value(argv) if callable(value) else value
                return json.dumps(found)
        return json.dumps([] if method == "GET" and "/comments" in path else {})

    def writes(self) -> list[list[str]]:
        return [call for call in self.calls if "--method" in call]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    found = Fake()
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None: found(self, args))
    return found


def issue(number: int, title: str, body: str = "", labels: Sequence[str] = (), **extra: object):
    row: dict[str, object] = {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": label} for label in labels],
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "state": "open",
        "updated_at": "2026-11-20T00:00:00Z",
        "created_at": "2026-11-01T00:00:00Z",
    }
    row.update(extra)
    return row


def pr(number: int, title: str, body: str = "", labels: Sequence[str] = ()):
    return issue(number, title, body, labels, pull_request={"url": "x"})


# inventory -----------------------------------------------------------------------------


def test_inventory_reads_every_page_not_just_the_first_hundred(fake: Fake) -> None:
    first = [issue(n, f"Issue {n}") for n in range(1, 101)]
    second = [issue(101, "Issue 101"), pr(102, "Fix", "Closes #5")]
    fake.routes[r"issues\?state=open.*page=1$"] = first
    fake.routes[r"issues\?state=open.*page=2$"] = second
    fake.routes[r"pulls/102$"] = {"head": {"sha": "a" * 40, "ref": "factory/x"}}
    found = groom.inventory(GitHub(REPO), timelines=False)
    assert len(found.issues) == 101 and len(found.prs) == 1
    assert found.to_dict()["totals"] == {"issues": 101, "prs": 1}


def test_inventory_fails_closed_when_a_page_cannot_be_read(fake: Fake) -> None:
    fake.routes[r"issues\?state=open.*page=1$"] = [issue(n, "x") for n in range(1, 101)]

    def broken(_argv: list[str]) -> JSON:
        raise GitHubError("GitHub request failed: HTTP 502")

    fake.routes[r"issues\?state=open.*page=2$"] = broken
    with pytest.raises(groom.InventoryError, match="page 2"):
        groom.inventory(GitHub(REPO), timelines=False)


def test_inventory_evidence_links_dependencies_duplicates_and_superseded_prs(fake: Fake) -> None:
    fake.routes[r"issues\?state=open.*page=1$"] = [
        issue(10, "Queue count shows wrong total after read", "Depends on #3"),
        issue(11, "Queue count shows the wrong total after a read"),
        issue(12, "Factory worker lease leaks on cancel"),
        issue(13, "Owned work", labels=[OWNED]),
        pr(20, "Old fix", "Closes #4"),
        pr(21, "Current fix", "Part of #13"),
    ]
    fake.routes[r"pulls/20$"] = {"head": {"sha": "b" * 40, "ref": "factory/old"}}
    fake.routes[r"pulls/21$"] = {"head": {"sha": "c" * 40, "ref": "factory/new"}}
    fake.routes[r"issues/3$"] = {"state": "closed"}
    fake.routes[r"issues/4$"] = {"state": "closed"}
    found = groom.inventory(GitHub(REPO), timelines=False)
    items = {item.number: item for item in (*found.issues, *found.prs)}
    codes = {n: {signal.code for signal in item.signals} for n, item in items.items()}
    assert "closed-dependency" in codes[10]
    assert "similar-title" in codes[10] and "similar-title" in codes[11]
    assert "runtime-text" in codes[12] and items[12].disposition == "unknown"
    assert "held" in codes[13] and "open-pr" in codes[13]
    assert items[13].implemented_by == (21,)
    assert items[20].disposition == "superseded"  # its only issue is closed
    assert items[21].disposition == "active"


def test_a_merged_pr_is_evidence_to_verify_never_a_closure(fake: Fake) -> None:
    fake.routes[r"issues\?state=open.*page=1$"] = [issue(30, "Bug")]
    fake.routes[r"issues/30/timeline"] = [
        {
            "event": "cross-referenced",
            "source": {"issue": {"number": 40, "title": "Fix", "body": "Closes #30",
                                 "pull_request": {"merged_at": "2026-11-19T00:00:00Z"}}},
        },
        {
            "event": "cross-referenced",
            "source": {"issue": {"number": 41, "title": "Mention", "body": "see #30",
                                 "pull_request": {"merged_at": "2026-11-19T00:00:00Z"}}},
        },
    ]  # fmt: skip
    found = groom.inventory(GitHub(REPO))
    [item] = found.issues
    [signal] = [s for s in item.signals if s.code == "merged-pr"]
    assert signal.refs == (40,)  # a bare mention is not a delivery claim
    assert item.disposition == "unknown"  # needs verification
    assert not fake.writes()


# plans ---------------------------------------------------------------------------------


def plan(*actions: dict[str, object]) -> list[groom.Action]:
    return groom.parse_plan(json.dumps({"actions": list(actions)}))


def test_every_action_needs_evidence_and_the_right_target() -> None:
    with pytest.raises(groom.PlanError, match="needs a why"):
        plan({"op": "comment", "target": "issue:1", "comment": "x"})
    with pytest.raises(groom.PlanError, match="applies to pr"):
        plan({"op": "close-pr", "target": "issue:1", "why": "w", "comment": "c"})
    with pytest.raises(groom.PlanError, match="expect.body_sha256"):
        plan({"op": "edit-body", "target": "issue:1", "why": "w", "body": "new"})
    with pytest.raises(groom.PlanError, match="expect.head"):
        plan({"op": "close-pr", "target": "pr:1", "why": "w", "comment": "c"})
    with pytest.raises(groom.PlanError, match="appears twice"):
        plan(
            {"id": "a", "op": "comment", "target": "issue:1", "why": "w", "comment": "c"},
            {"id": "a", "op": "comment", "target": "issue:2", "why": "w", "comment": "c"},
        )


def live(fake: Fake, number: int, body: str = "old", labels: Sequence[str] = (), **extra: object):
    fake.routes[rf"issues/{number}$"] = issue(number, "t", body, labels, **extra)


def test_dry_run_changes_nothing(fake: Fake) -> None:
    live(fake, 1)
    actions = plan(
        {"op": "close-issue", "target": "issue:1", "why": "done in #9", "reason": "completed",
         "comment": "Done."}
    )  # fmt: skip
    [result] = groom.apply(GitHub(REPO), actions, write=False)
    assert result.status == "would-apply"
    assert not fake.writes()


def test_apply_closes_with_a_comment_and_marker(fake: Fake) -> None:
    live(fake, 1)
    actions = plan(
        {"id": "close-1", "op": "close-issue", "target": "issue:1", "why": "done in #9",
         "reason": "completed", "comment": "Done in #9."}
    )  # fmt: skip
    [result] = groom.apply(GitHub(REPO), actions, write=True)
    assert result.status == "applied"
    comment, close = fake.writes()
    assert "Done in #9." in " ".join(comment) and "rotisserie-groom:v1:close-1" in " ".join(comment)
    assert "state=closed" in close and "state_reason=completed" in close


def test_apply_is_idempotent(fake: Fake) -> None:
    live(fake, 1)
    fake.routes[r"issues/1/comments"] = [{"body": "Done.\n\n<!-- rotisserie-groom:v1:close-1 -->"}]
    actions = plan(
        {"id": "close-1", "op": "close-issue", "target": "issue:1", "why": "w",
         "reason": "completed", "comment": "Done."}
    )  # fmt: skip
    [result] = groom.apply(GitHub(REPO), actions, write=True)
    assert result.status == "skipped" and not fake.writes()


def test_apply_refuses_items_a_worker_holds(fake: Fake) -> None:
    live(fake, 1, labels=[OWNED])
    actions = plan({"op": "labels", "target": "issue:1", "why": "w", "remove": ["factory:blocked"]})
    [result] = groom.apply(GitHub(REPO), actions, write=True)
    assert result.status == "refused" and OWNED in result.text
    assert not fake.writes()


def test_apply_refuses_a_body_that_changed_since_review(fake: Fake) -> None:
    live(fake, 1, body="someone edited this")
    actions = plan(
        {"op": "edit-body", "target": "issue:1", "why": "w", "body": "new",
         "expect": {"body_sha256": groom.body_hash("old")}}
    )  # fmt: skip
    [result] = groom.apply(GitHub(REPO), actions, write=True)
    assert result.status == "refused" and "body changed" in result.text
    assert not fake.writes()


def test_edit_body_writes_the_reviewed_replacement(fake: Fake) -> None:
    live(fake, 1, body="old")
    actions = plan(
        {"op": "edit-body", "target": "issue:1", "why": "hold lifted", "body": "new body",
         "expect": {"body_sha256": groom.body_hash("old")}}
    )  # fmt: skip
    [result] = groom.apply(GitHub(REPO), actions, write=True)
    assert result.status == "applied"
    edit = fake.writes()[0]
    assert "PATCH" in edit and "body=new body" in edit


def test_closing_a_pr_needs_its_reviewed_head_and_never_closes_its_issue(fake: Fake) -> None:
    fake.routes[r"issues/7$"] = pr(7, "t", "Closes #3") | {"state": "open"}
    fake.routes[r"pulls/7$"] = {"head": {"sha": "d" * 40}}
    moved = plan(
        {"op": "close-pr", "target": "pr:7", "why": "w", "comment": "c",
         "expect": {"head": "e" * 40}}
    )  # fmt: skip
    assert groom.apply(GitHub(REPO), moved, write=True)[0].status == "refused"
    ok = plan(
        {"op": "close-pr", "target": "pr:7", "why": "w", "comment": "superseded by #8",
         "expect": {"head": "d" * 40}}
    )  # fmt: skip
    assert groom.apply(GitHub(REPO), ok, write=True)[0].status == "applied"
    touched = {next(p for p in call if p.startswith("repos/")) for call in fake.writes()}
    assert touched == {f"repos/{REPO}/issues/7/comments", f"repos/{REPO}/pulls/7"}


def test_an_issue_an_open_pr_still_implements_is_not_closed(fake: Fake) -> None:
    live(fake, 3)
    fake.routes[r"issues/3/timeline"] = [
        {"event": "cross-referenced",
         "source": {"issue": {"number": 7, "state": "open", "title": "Fix", "body": "Closes #3",
                              "pull_request": {"url": "x"}}}}
    ]  # fmt: skip
    actions = plan(
        {"op": "duplicate", "target": "issue:3", "why": "same as #2", "duplicate_of": 2,
         "comment": "Same defect."}
    )  # fmt: skip
    [result] = groom.apply(GitHub(REPO), actions, write=True)
    assert result.status == "refused" and "#7 still implements" in result.text
    assert not fake.writes()


def test_duplicates_are_linked_then_closed(fake: Fake) -> None:
    live(fake, 3)
    actions = plan(
        {"op": "duplicate", "target": "issue:3", "why": "same as #2", "duplicate_of": 2,
         "comment": "Same defect; tracked in #2."}
    )  # fmt: skip
    [result] = groom.apply(GitHub(REPO), actions, write=True)
    assert result.status == "applied"
    comment, close = fake.writes()
    assert "Duplicate of #2." in " ".join(comment)
    assert "state_reason=not_planned" in close


def test_already_closed_items_are_skipped(fake: Fake) -> None:
    live(fake, 1, state="closed")
    actions = plan({"op": "comment", "target": "issue:1", "why": "w", "comment": "c"})
    assert groom.apply(GitHub(REPO), actions, write=True)[0].status == "skipped"


def test_groom_cli_dry_run_and_audit_log(fake: Fake, tmp_path: Path, monkeypatch) -> None:
    from rotisserie.factory_console import commands

    live(fake, 1)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"actions": [
        {"op": "comment", "target": "issue:1", "why": "w", "comment": "c"}]}))  # fmt: skip
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NO_COLOR", "1")
    code = commands.run_cli(
        ["factory", "--repo", REPO, "groom", "apply", str(path), "--log", str(log)]
    )
    assert code == 0 and not fake.writes()
    [entry] = [json.loads(line) for line in log.read_text().splitlines()]
    assert entry["status"] == "would-apply" and entry["repo"] == REPO


def test_transfer_moves_a_runtime_issue_upstream_after_the_same_checks(fake: Fake) -> None:
    live(fake, 5)
    actions = plan(
        {"op": "transfer", "target": "issue:5", "to_repo": "acme/runtime",
         "why": "it is about the factory runtime", "comment": "Moving upstream."}
    )  # fmt: skip
    [result] = groom.apply(GitHub(REPO), actions, write=True)
    assert result.status == "applied"
    assert ["issue", "transfer", "5", "acme/runtime", "--repo", REPO] in fake.calls
    with pytest.raises(groom.PlanError, match="to_repo"):
        plan({"op": "transfer", "target": "issue:5", "why": "w", "comment": "c"})
