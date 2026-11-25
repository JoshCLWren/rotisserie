"""The factory clears empty repair verdicts, answered bot threads and stale conflicts by itself."""

from __future__ import annotations

import json
from typing import Any

import factory_review_policy as policy
import factory_self_heal as heal
import factory_self_heal_actions as actions
import factory_self_heal_rules as rules
import pytest

HEAD = "b" * 40
OLD = "a" * 40
FIX = "c" * 40
NARRATION = "I'll start by reading the AGENTS.md file.\n$ git log -3\n(no output)\ngh pr diff 44"
FINDING = "The retry loop never resets its counter, so the second failure aborts the job."
ACTIONS_BOT = {"login": "github-actions[bot]", "type": "Bot"}


def verdict(
    excerpt: str, *, verdict: str = "repair", head: str = OLD, cid: int = 7
) -> dict[str, Any]:
    marker = policy.review_marker(pr=44, head=head, reviewer="123", producer=None, verdict=verdict)
    body = (
        f"{marker}\n\n### Factory semantic review · Factory 123 · REPAIR\n\nSemantic blockers remain."
        f"\n\n<details><summary>Review output</summary>\n\n```text\n{excerpt}\n```\n</details>"
    )
    return {"id": cid, "user": ACTIONS_BOT, "body": body, "html_url": f"https://x/c{cid}"}


def handoff(model: str, *, head: str = OLD, worker: str = "123") -> dict[str, Any]:
    body = (
        "### Factory handoff · review\n\n"
        f"Factory worker {worker} finished a semantic review at `{head[:12]}`.\n\n"
        f"Semantic verdict: repair.\n\nWorker: opencode-free · {model} · Model: {model}\n"
    )
    return {"id": 8, "user": {"login": "rotisserie-factory[bot]", "type": "Bot"}, "body": body}


def trusted(body: str, cid: int = 9) -> dict[str, Any]:
    return {"id": cid, "user": ACTIONS_BOT, "body": body}


class Hub:
    repo = "acme/widgets"

    def __init__(self) -> None:
        self.records: dict[int, list[dict[str, Any]]] = {}
        self.issue_labels: dict[int, list[str]] = {}
        self.pull_rows: list[dict[str, Any]] = []
        self.live: dict[int, dict[str, Any]] = {}
        self.threads: list[dict[str, Any]] = []
        self.compare_status = "ahead"
        self.writes: list[tuple[str, Any]] = []
        self.resolved: list[str] = []

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if "--method" in args:
            self.writes.append((path, payload))
            if path.endswith("/comments"):
                number = int(path.split("/")[1])
                body = str((payload or {}).get("body"))  # type: ignore[union-attr]
                self.records.setdefault(number, []).append(trusted(body, 100 + len(self.writes)))
            return {}
        if path.startswith("pulls?state=open"):
            return [self.pull_rows]
        if path.startswith("issues/"):
            number = int(path.split("/")[1])
            labels = [{"name": name} for name in self.issue_labels.get(number, [])]
            return {"number": number, "labels": labels, "body": ""}
        if path.startswith("pulls/"):
            return self.live[int(path.split("/")[1])]
        if path.startswith("compare/"):
            return {"status": self.compare_status}
        raise AssertionError(path)

    def comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.records.get(number, []))

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        if "resolveReviewThread" in query:
            self.resolved.append(str(variables["thread"]))
            return {"data": {}}
        page = {"pageInfo": {"hasNextPage": False}, "nodes": self.threads}
        return {"data": {"repository": {"pullRequests": page}}}


class Store:
    def __init__(self, *, enabled: bool = True, holder: str | None = None) -> None:
        self.on = enabled
        self.held = holder
        self.events: list[dict[str, Any]] = []

    def enabled(self) -> bool:
        return self.on

    def record(self, entries: list[dict[str, Any]]) -> int:
        fresh = [e for e in entries if e["event_id"] not in {x["event_id"] for x in self.events}]
        self.events += fresh
        return len(fresh) if self.on else 0

    def holder(self, number: int) -> str | None:
        return self.held

    def loop_evidence(self, seconds: int) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}


def pull(labels: list[str], *, head: str = HEAD) -> dict[str, Any]:
    names = ["factory", *labels]
    return {
        "number": 44,
        "head": {"sha": head},
        "base": {"ref": "main"},
        "labels": [{"name": n} for n in names],
    }


def budget(**overrides: Any) -> actions.Budget:
    return actions.Budget(**{"actions": 10, "syncs": 3, **overrides})


# Empty repair verdict ---------------------------------------------------------


def test_an_empty_verdict_names_the_model_that_wrote_it() -> None:
    found = rules.empty_verdict([verdict(NARRATION), handoff("iceman-contributor-free")], 44)
    assert found is not None
    assert (found.verdict, found.head, found.ref) == ("repair", OLD, "7")
    assert found.model == "iceman-contributor-free"


@pytest.mark.parametrize(
    "records",
    [
        [verdict(FINDING)],
        [verdict(NARRATION), verdict(FINDING, verdict="approve", cid=8)],
        # A newer controller repair handoff always carries findings.
        [verdict(NARRATION), trusted("### Factory semantic review · Factory controller · REPAIR")],
    ],
)
def test_other_histories_are_not_empty_verdicts(records: list[dict[str, Any]]) -> None:
    assert rules.empty_verdict(records, 44) is None


def test_a_verdict_healed_once_is_still_found_but_marked_healed() -> None:
    heal_body = rules.comment_body(kind=rules.EMPTY_VERDICT, pr=44, head=HEAD, ref="7", text="x")
    found = rules.empty_verdict([verdict(NARRATION), trusted(heal_body)], 44)
    assert found is not None and found.healed


def test_the_sweep_moves_it_back_to_review_once_and_says_why() -> None:
    hub, store = Hub(), Store()
    hub.records[44] = [verdict(NARRATION), handoff("iceman-contributor-free")]
    hub.issue_labels[44] = ["factory", "factory:unowned", "factory:changes-requested", "bug"]
    pulls = [pull(["factory:unowned", "factory:changes-requested"])]

    [done] = actions.heal_empty_verdicts(hub, store, pulls, budget())

    path, payload = hub.writes[0]
    assert path == "issues/44/labels"
    assert payload == {"labels": ["bug", "factory", "factory:review", "factory:unowned"]}
    body = hub.writes[1][1]["body"]
    assert rules.parse_marker(body)["avoid"] == "iceman-contributor-free"  # type: ignore[index]
    assert "Moved #44 back to review" in done["text"]
    assert store.events[0]["kind"] == rules.EMPTY_VERDICT
    assert rules.avoided_reviewers(hub.records[44], pr=44) == ["iceman-contributor-free"]

    assert actions.heal_empty_verdicts(hub, store, pulls, budget()) == []


@pytest.mark.parametrize(
    ("labels", "holder"),
    [
        (["factory:1234", "factory:changes-requested"], None),
        (["factory:claimed", "factory:changes-requested"], None),
        (["factory:unowned", "factory:changes-requested"], "worker 77"),
    ],
)
def test_a_held_pr_is_never_moved(labels: list[str], holder: str | None) -> None:
    hub = Hub()
    hub.records[44] = [verdict(NARRATION)]
    hub.issue_labels[44] = ["factory", *labels]
    actions.heal_empty_verdicts(hub, Store(holder=holder), [pull(labels)], budget())
    assert hub.writes == []


# Answered bot threads ---------------------------------------------------------


def comment(login: str, body: str, *, bot: bool = True) -> dict[str, Any]:
    kind = "Bot" if bot else "User"
    return {
        "author": {"login": login, "__typename": kind},
        "body": body,
        "commit": {"oid": HEAD},
        "url": "https://x/r",
    }


def thread(*comments: dict[str, Any], outdated: bool = False) -> dict[str, Any]:
    return {
        "id": "PRRT_1",
        "isResolved": False,
        "isOutdated": outdated,
        "path": "src/a.ts",
        "comments": {"pageInfo": {"hasNextPage": False}, "nodes": list(comments)},
    }


GREPTILE = comment("greptile-apps", "P0: the test never awaits the refresh")
OUTCOME = f"<!-- {rules.OUTCOME_MARKER}:PRRT_1:fixed:{HEAD}:abc -->"
FIXED = comment("github-actions", f"**Fixed** in `{FIX}` by Factory 100.\n\n{OUTCOME}")


def pr_node(*threads: dict[str, Any], stage: str = "factory:review") -> dict[str, Any]:
    return {
        "number": 44,
        "headRefOid": HEAD,
        "labels": {"nodes": [{"name": stage}]},
        "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": list(threads)},
    }


def test_an_answered_bot_thread_is_resolved_and_recorded() -> None:
    hub, store = Hub(), Store()
    hub.threads = [pr_node(thread(GREPTILE, FIXED))]
    [done] = actions.resolve_answered_threads(hub, store, [], budget())
    assert hub.resolved == ["PRRT_1"]
    assert "Resolved greptile-apps's thread on src/a.ts in #44" in done["text"]
    marker = rules.parse_marker(hub.records[44][0]["body"])
    assert marker is not None and marker["kind"] == rules.THREAD_RESOLVED


@pytest.mark.parametrize(
    "shape",
    [
        thread(comment("JoshCLWren", "Please rename this", bot=False), FIXED),
        thread(GREPTILE, comment("JoshCLWren", "I disagree", bot=False), FIXED),
        thread(GREPTILE, comment("github-actions", "**Rebuttal** (no code change) by Factory 1")),
        thread(GREPTILE, FIXED, comment("greptile-apps", "Still broken.")),
        thread(GREPTILE, FIXED, outdated=True),
        thread(GREPTILE, comment("github-actions", f"**Fixed** in `{FIX}` by Factory 1.")),
        thread(GREPTILE, comment("github-actions", f"**Fixed** in `{FIX}`.\n\n{OUTCOME.replace('PRRT_1', 'PRRT_2')}")),
    ],
)
def test_person_threads_rebuttals_and_reopened_threads_stay_open(shape: dict[str, Any]) -> None:
    hub = Hub()
    hub.threads = [pr_node(shape)]
    actions.resolve_answered_threads(hub, Store(), [], budget())
    assert hub.resolved == [] and hub.writes == []


def test_a_fix_that_is_not_in_the_head_leaves_the_thread_open() -> None:
    hub = Hub()
    hub.threads = [pr_node(thread(GREPTILE, FIXED))]
    hub.compare_status = "diverged"
    [done] = actions.resolve_answered_threads(hub, Store(), [], budget())
    assert done["status"] == "fix-not-in-head" and hub.resolved == []


# Merge conflicts --------------------------------------------------------------


class Outcome:
    def __init__(self, status: str) -> None:
        self.status = status
        self.text = status


def conflicted(hub: Hub, *, mergeable: bool | None) -> list[dict[str, Any]]:
    hub.live[44] = {"mergeable": mergeable, "mergeable_state": "clean", "head": {"sha": HEAD}}
    return [pull(["factory:unowned", "factory:merge-conflict"])]


def test_a_conflict_main_now_merges_past_is_synced_once_per_head() -> None:
    import factory_branch_sync as branch_sync

    hub, store, calls = Hub(), Store(), []

    def sync(module: Any, repo: str, view: Any) -> Outcome:
        calls.append((repo, view.number, view.head, view.base))
        return Outcome(module.SYNCED)

    pulls = conflicted(hub, mergeable=True)
    [done] = actions.sync_conflicts(hub, store, pulls, budget(), sync=sync)
    assert calls == [("acme/widgets", 44, HEAD, "main")]
    assert "Merged main into #44" in done["text"]
    assert branch_sync.SYNCED == "synced"

    assert actions.sync_conflicts(hub, store, pulls, budget(), sync=sync) == []
    assert len(calls) == 1


def test_a_real_conflict_is_left_to_a_model_worker_and_recorded() -> None:
    hub, store = Hub(), Store()
    pulls = conflicted(hub, mergeable=False)
    [done] = actions.sync_conflicts(
        hub, store, pulls, budget(), sync=lambda *_: pytest.fail("sync")
    )
    assert done["status"] == "needs-judgment"
    assert store.events[0]["event_id"] == f"self-heal:conflict-attempt:44:{HEAD}"
    assert hub.writes == []


@pytest.mark.parametrize(
    ("store", "labels", "mergeable"),
    [
        (Store(enabled=False), ["factory:unowned", "factory:merge-conflict"], True),
        (Store(holder="worker 9"), ["factory:unowned", "factory:merge-conflict"], True),
        (Store(), ["factory:9", "factory:merge-conflict"], True),
        (Store(), ["factory:claimed", "factory:merge-conflict"], True),
        (Store(), ["factory:unowned", "factory:merge-conflict"], None),
    ],
)
def test_a_held_or_unknown_conflict_is_never_synced(
    store: Store, labels: list[str], mergeable: bool | None
) -> None:
    hub = Hub()
    hub.live[44] = {"mergeable": mergeable, "head": {"sha": HEAD}}
    found = actions.sync_conflicts(
        hub, store, [pull(labels)], budget(), sync=lambda *_: pytest.fail("sync")
    )
    assert found == [] and store.events == []


def test_a_dry_run_changes_nothing() -> None:
    hub, store = Hub(), Store()
    hub.records[44] = [verdict(NARRATION)]
    hub.issue_labels[44] = ["factory", "factory:unowned", "factory:changes-requested"]
    hub.pull_rows = [pull(["factory:unowned", "factory:changes-requested"])]
    hub.threads = [pr_node(thread(GREPTILE, FIXED))]
    result = heal.sweep(hub, store, dry_run=True)
    assert [row["status"] for row in result["verdicts"]] == ["would-heal"]
    assert [row["status"] for row in result["threads"]] == ["would-heal"]
    assert hub.writes == [] and hub.resolved == [] and store.events == []
    assert json.dumps(result)


def test_the_store_records_one_conflict_attempt_per_head(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from tests import turso_support as support

    events = importlib.import_module("factory_turso_events")

    def attempt(head: str) -> dict[str, Any]:
        row = heal.event(kind=rules.CONFLICT_SYNC, pr=44, head=head, ref=head, text="attempt")
        return {**row, "event_id": f"self-heal:conflict-attempt:44:{head}"}

    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (_db, repo):
        assert events.record_events(repo, [attempt(HEAD)]) == 1
        assert events.record_events(repo, [attempt(HEAD)]) == 0
        assert events.record_events(repo, [attempt(OLD)]) == 1


@pytest.mark.parametrize(
    ("runtime_model", "note_model"),
    [
        ("opencode-go/iceman-contributor-free", "iceman-contributor-free"),
        ("nvidia/moonshotai/kimi-k3.1-pro", "moonshotai/kimi-k3.1-pro"),
    ],
)
def test_the_review_guard_matches_a_runtime_model_to_the_avoided_note_model(
    runtime_model: str, note_model: str
) -> None:
    import factory_review_model_guard as guard

    body = rules.comment_body(
        kind=rules.EMPTY_VERDICT, pr=44, head=HEAD, ref="7", text="x", avoid=note_model
    )
    avoided = rules.avoided_reviewers([trusted(body)], pr=44)
    assert guard.self_review(runtime_model, avoided) == [note_model]
