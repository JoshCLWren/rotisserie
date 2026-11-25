"""Line-anchored review findings: parsing, validation, verdict, posting, repair handoff."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import factory_review_findings as findings
import pytest
from pytest import MonkeyPatch
from tests.test_factory_review_gate import (
    REVIEWED_HEAD,
    load_review_controller,
    pr_payload,
    wire_controller,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_5674 = ROOT / "tests/fixtures/review_5674_empty_verdict.txt"
PATCH = (
    "@@ -10,3 +10,5 @@ def run():\n ctx\n+added\n+added\n ctx\n ctx\n@@ -40,2 +42,2 @@\n-x\n+y\n z"
)
FILES = [
    {"filename": "app/run.py", "status": "modified", "patch": PATCH},
    {"filename": "app/logo.png", "status": "added"},
]


def record(*items: dict[str, object], head: str = REVIEWED_HEAD) -> str:
    return (
        "the review prose\n" + findings.PREFIX + json.dumps({"head": head, "findings": list(items)})
    )


def item(
    line: int, *, path: str = "app/run.py", severity: str = "blocking", **extra: object
) -> dict[str, object]:
    return {"path": path, "line": line, "severity": severity, "body": "Null check missing", **extra}


def check(text: str, files: list[dict[str, Any]] = FILES) -> findings.Review:
    parsed, error = findings.parse_record(text, head=REVIEWED_HEAD)
    return findings.validate(parsed, error, files)


def test_finding_on_a_changed_line_is_valid() -> None:
    review = check(record(item(11), item(43, severity="non-blocking")))
    assert [found.location() for found in review.valid] == ["app/run.py:11", "app/run.py:43"]
    assert review.dropped == ()


@pytest.mark.parametrize(
    ("finding", "reason"),
    [
        (item(11, path="app/other.py"), "path is not changed by this PR"),
        (item(30), "line is outside every diff hunk of the head"),
        (item(5), "line is outside every diff hunk of the head"),
        (item(43, start_line=12), "range crosses a diff hunk boundary"),
        (item(3, path="app/logo.png"), "line is outside every diff hunk of the head"),
        (item(11, severity="major"), "severity must be blocking or non-blocking"),
        (item(11, side="LEFT"), "only the head (RIGHT) side can be commented"),
        ({"path": "app/run.py", "line": 11, "severity": "blocking"}, "no explanation"),
    ],
)
def test_finding_off_the_head_diff_is_dropped_and_counted(
    finding: dict[str, object], reason: str
) -> None:
    review = check(record(finding))
    assert review.valid == ()
    assert [drop.reason for drop in review.dropped] == [reason]
    assert findings.decide(review) == findings.APPROVE


def test_range_inside_one_hunk_is_valid() -> None:
    review = check(record(item(14, start_line=10)))
    assert review.valid[0].location() == "app/run.py:10-14"


def test_verdict_is_mechanical() -> None:
    assert findings.decide(check(record())) == findings.APPROVE
    assert findings.decide(check(record(item(11, severity="non-blocking")))) == findings.APPROVE
    assert findings.decide(check(record(item(11)))) == findings.REPAIR
    assert findings.decide(check(record(item(11))), rejects=True) == findings.REJECT
    assert findings.decide(check(record()), rejects=True) == findings.APPROVE


@pytest.mark.parametrize(
    "text",
    [
        "Looks good to me.\nFACTORY_GATE_READY",
        record(item(11), head="b" * 40),
        record(item(11)) + "\n" + record(item(12)).splitlines()[-1],
        findings.PREFIX + "{not json",
        "prose that mentions " + findings.PREFIX + '{"head":"x","findings":[]}',
    ],
)
def test_no_parseable_record_is_a_failed_run(text: str) -> None:
    review = check(text)
    assert not review.parsed
    assert findings.decide(review) == findings.FAILED


def test_record_inside_an_opencode_text_event_parses() -> None:
    event = {"type": "text", "part": {"type": "text", "text": record(item(11))}}
    assert findings.decide(check(json.dumps(event))) == findings.REPAIR


def test_the_mission_example_cannot_parse_as_output() -> None:
    assert not check(findings.mission(REVIEWED_HEAD)).parsed


def test_inline_payload_is_anchored_by_path_line_and_side() -> None:
    review = check(
        record(
            item(14, start_line=11, body="Use <!-- x --> @owner"), item(43, severity="non-blocking")
        )
    )
    payload = findings.review_payload(
        pr=7, head=REVIEWED_HEAD, reviewer="41", verdict="repair", review=review
    )
    assert payload["commit_id"] == REVIEWED_HEAD
    assert payload["event"] == "COMMENT"
    first, second = payload["comments"]
    assert {key: first[key] for key in ("path", "line", "side", "start_line", "start_side")} == {
        "path": "app/run.py",
        "line": 14,
        "side": "RIGHT",
        "start_line": 11,
        "start_side": "RIGHT",
    }
    assert "position" not in first and "start_line" not in second
    marker = findings.FINDING_MARKER_RE.match(first["body"])
    assert marker and marker.group("start") == "11" and marker.group("end") == "14"
    assert "<!-- x" not in first["body"] and "@owner" not in first["body"]
    summary = findings.SUMMARY_MARKER_RE.match(payload["body"])
    assert summary and summary.group("blocking") == "1" and summary.group("valid") == "2"
    posted = [{"path": "app/run.py", "line": 14}, {"path": "app/run.py", "line": None}]
    assert findings.unanchored(posted, payload["comments"]) == ["app/run.py:43"]


def factory_thread(
    head: str, *, resolved: bool = False, start: int = 11, end: int = 11
) -> dict[str, Any]:
    finding = findings.Finding(
        path="app/run.py",
        line=end,
        start_line=start if start != end else None,
        severity="blocking",
        body="x",
    )
    return {
        "id": f"PRRT_{head[:3]}",
        "isResolved": resolved,
        "isOutdated": False,
        "path": "app/run.py",
        "comments": {
            "nodes": [
                {
                    "author": {"login": "github-actions", "__typename": "Bot"},
                    "body": findings.finding_marker(pr=7, head=head, finding=finding) + "\nx",
                }
            ]
        },
    }


def test_new_review_supersedes_older_factory_threads() -> None:
    old, current = factory_thread("b" * 40), factory_thread(REVIEWED_HEAD)
    greptile = {
        "id": "PRRT_g",
        "isResolved": False,
        "comments": {
            "nodes": [
                {"author": {"login": "greptile-apps"}, "body": old["comments"]["nodes"][0]["body"]}
            ]
        },
    }
    threads = [old, current, greptile, factory_thread("c" * 40, resolved=True)]
    assert findings.superseded_threads(threads, pr=7, head=REVIEWED_HEAD, verdict="repair") == [
        "PRRT_bbb"
    ]
    assert findings.superseded_threads(threads, pr=7, head=REVIEWED_HEAD, verdict="approve") == [
        "PRRT_bbb",
        "PRRT_aaa",
    ]
    assert findings.superseded_threads(threads, pr=8, head=REVIEWED_HEAD, verdict="approve") == []


def test_fix_touched_reads_the_old_side_of_the_compare() -> None:
    compare = [{"filename": "app/run.py", "patch": "@@ -20,4 +20,6 @@\n ctx"}]
    assert findings.fix_touched(compare, path="app/run.py", first=18, last=21)
    assert not findings.fix_touched(compare, path="app/run.py", first=11, last=14)
    assert not findings.fix_touched(compare, path="app/other.py", first=20, last=20)
    assert findings.fix_touched(
        [{"filename": "app/run.py", "status": "removed"}], path="app/run.py", first=1, last=1
    )


class FakeGitHub:
    """Routes the controller's gh_json calls; records every write."""

    def __init__(
        self,
        *,
        files: list[dict[str, Any]] = FILES,
        fail_post: bool = False,
        anchor: bool = True,
        threads: list[dict[str, Any]] | None = None,
    ) -> None:
        self.files, self.fail_post, self.anchor = files, fail_post, anchor
        self.threads = threads or []
        self.posted: list[Any] = []
        self.resolved: list[str] = []

    def __call__(self, args: list[str], *, input_json: object | None = None) -> object:
        path = next(
            (arg for arg in args if arg.startswith("repos/") or arg.startswith("query=")), ""
        )
        if path.endswith("/files?per_page=100"):
            return [self.files]
        if path.endswith("/reviews") and "POST" in args:
            if self.fail_post:
                raise RuntimeError("gh api failed (1): HTTP 422 Unprocessable Entity")
            self.posted.append(input_json)
            return {"id": 99}
        if path.endswith("/comments?per_page=100"):
            comments = self.posted[-1]["comments"] if self.posted else []
            return [
                [
                    {
                        "pull_request_review_id": 99,
                        "path": row["path"],
                        "line": row["line"] if self.anchor else None,
                    }
                    for row in comments
                ]
            ]
        if "resolveReviewThread" in path:
            self.resolved.append(
                next(arg for arg in args if arg.startswith("thread=")).split("=", 1)[1]
            )
            return {}
        if "reviewThreads" in path:
            return {
                "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": self.threads}}}}
            }
        raise AssertionError(f"unexpected gh call {args}")


def wire(
    monkeypatch: MonkeyPatch, log: str, github: FakeGitHub
) -> tuple[Any, list[dict[str, object]], list[dict[str, object]]]:
    module = load_review_controller()
    transitions, posted, _commands = wire_controller(
        monkeypatch, module, [pr_payload(worker="17"), pr_payload(worker="17")]
    )
    monkeypatch.setattr(module, "trusted_review_log", lambda _path, **_kw: log)
    monkeypatch.setattr(module, "gh_json", github)
    monkeypatch.setattr(module, "semantic_repair_heads", lambda *_a, **_kw: set())
    monkeypatch.setattr(module, "attest_exact_head_author", lambda **_kw: None)
    monkeypatch.setattr(module, "persist_cross_model_review_marker", lambda **_kw: None)
    return module, transitions, posted


def review(module: Any, verdict: str) -> dict[str, Any]:
    return dict(
        module.handle_review(
            worker="17",
            pr_number=7,
            verdict=verdict,
            reviewed_head=REVIEWED_HEAD,
            review_log="/tmp/opencode-factory-17.sanitized.log",
        )
    )


def test_blocking_findings_request_changes_and_post_inline(monkeypatch: MonkeyPatch) -> None:
    github = FakeGitHub(threads=[factory_thread("b" * 40)])
    module, transitions, posted = wire(
        monkeypatch, record(item(11), item(43, severity="non-blocking")), github
    )
    result = review(module, "approve")
    assert result["status"] == "repair"
    assert transitions[-1]["pr_stage"] == "factory:changes-requested"
    assert [row["line"] for row in github.posted[0]["comments"]] == [11, 43]
    assert github.resolved == ["PRRT_bbb"]
    handoff = str(posted[-1]["excerpt"])
    assert "[blocking] app/run.py:11: Null check missing" in handoff


def test_zero_blocking_findings_approve_despite_reject_prose(monkeypatch: MonkeyPatch) -> None:
    log = "This PR is wrong.\nFACTORY_GATE_NOT_READY\n" + record(item(30))
    github = FakeGitHub(threads=[factory_thread(REVIEWED_HEAD)])
    module, transitions, _posted = wire(monkeypatch, log, github)
    result = review(module, "approve")
    assert result["status"] == "ready"
    assert transitions[-1]["pr_stage"] == "factory:ready"
    summary = findings.SUMMARY_MARKER_RE.match(github.posted[0]["body"])
    assert summary and summary.group("verdict") == "approve" and summary.group("dropped") == "1"
    assert github.resolved == ["PRRT_aaa"]


def test_wrapper_downgrade_still_needs_an_actionable_reason(monkeypatch: MonkeyPatch) -> None:
    module, transitions, _posted = wire(monkeypatch, record(), FakeGitHub())
    monkeypatch.setattr(module, "review_excerpt", lambda *_a, **_kw: "gh pr diff 7")
    assert review(module, "repair")["status"] == "insufficient-review-findings"
    module, transitions, _posted = wire(monkeypatch, record(), FakeGitHub())
    reason = "The trusted wrapper downgraded this approval: the pre-push fast checks failed."
    monkeypatch.setattr(module, "review_excerpt", lambda *_a, **_kw: reason)
    assert review(module, "repair")["status"] == "repair"
    assert transitions[-1]["pr_stage"] == "factory:changes-requested"


def test_replay_of_5674_empty_verdict_is_a_failed_run(monkeypatch: MonkeyPatch) -> None:
    log = FIXTURE_5674.read_text().replace("{prefix}", "acme")
    github = FakeGitHub()
    module, transitions, posted = wire(monkeypatch, log, github)
    result = review(module, "repair")
    assert result["status"] == "review-findings-missing"
    assert all(row.get("pr_stage") != "factory:changes-requested" for row in transitions)
    assert posted[-1]["verdict"] == "not-ready"
    assert github.posted == []


@pytest.mark.parametrize(
    ("github", "status"),
    [
        (FakeGitHub(fail_post=True), "review-findings-post-failed"),
        (FakeGitHub(anchor=False), "review-findings-post-failed"),
    ],
)
def test_unpublishable_review_is_never_an_approval(
    monkeypatch: MonkeyPatch, github: FakeGitHub, status: str
) -> None:
    module, transitions, _posted = wire(
        monkeypatch, record(item(11, severity="non-blocking")), github
    )
    result = review(module, "approve")
    assert result["status"] == status
    assert all(row.get("pr_stage") != "factory:ready" for row in transitions)


def run_shell(
    script: str, tmp_path: Path, files: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "files.json").write_text(json.dumps([files]))
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\ncat {tmp_path / 'files.json'}\n")
    gh.chmod(0o755)
    controller = ROOT / ".github/scripts/factory-review-controller.py"
    prelude = f'export PATH="{bin_dir}:$PATH" TRUSTED_REVIEW_CONTROLLER="{controller}"\n'
    prelude += f'source "{ROOT / ".github/scripts/factory-semantic-verdict.sh"}"\n'
    return subprocess.run(
        ["bash", "-c", prelude + script], text=True, capture_output=True, check=False, timeout=30
    )


def test_worker_verdict_comes_from_findings_not_markers(tmp_path: Path) -> None:
    log = tmp_path / "review.log"
    log.write_text(record(item(11)) + "\nFACTORY_GATE_READY\n")
    call = f'factory_line_review_verdict "{log}" 7 {REVIEWED_HEAD}'
    assert run_shell(call, tmp_path, FILES).stdout.strip() == "repair"
    log.write_text(record(item(30)) + "\nFACTORY_GATE_NOT_READY\n")
    assert run_shell(call, tmp_path, FILES).stdout.strip() == "approve"
    log.write_text(FIXTURE_5674.read_text())
    result = run_shell(call, tmp_path, FILES)
    assert result.returncode != 0 and result.stdout.strip() == ""


def test_review_stage_mission_carries_the_output_contract(tmp_path: Path) -> None:
    script = ROOT / ".github/scripts/factory-review-findings.sh"
    text = script.read_text()
    assert 'factory_review_findings.py" mission --head "$head"' in text
    assert '"${ASSIGNED_PR_STAGE:-}" != factory:review ]] || python3' in text
    fork = (ROOT / ".github/scripts/factory_fork_control.py").read_text()
    assert "__import__(\"factory_review_findings\").mission(head)" in fork


def test_repair_mission_lists_factory_findings_as_the_task(tmp_path: Path) -> None:
    from tests.test_factory_review_findings import format_findings, thread

    marker = findings.finding_marker(
        pr=42, head="b" * 40, finding=findings.Finding("app/x.py", 7, "blocking", "x")
    )
    out = format_findings(
        tmp_path,
        threads=[
            {**thread("github-actions", marker + "\n**Blocking:** Null check missing; guard it."),
             "comments": {"nodes": [{"author": {"login": "github-actions", "__typename": "Bot"},
                                     "body": marker + "\n**Blocking:** Null check missing; guard it.", "url": None}]}},
            thread("some-human", "Rename this", line=9),
        ],
    )
    task = out[out.index("## FACTORY REVIEW FINDINGS") : out.index("## HIGH PRIORITY")]
    assert "F1. app/x.py:7" in task and "Null check missing; guard it." in task
    other = out[out.index("## Other unresolved review threads") :]
    assert "Null check missing" not in other and "Rename this" in other


class CompareGitHub:
    def __init__(self, files: list[dict[str, Any]]) -> None:
        self.files = files
        self.paths: list[str] = []

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        self.paths.append(path)
        return {"files": self.files}


def test_self_heal_resolves_factory_threads_whose_lines_the_fix_changed() -> None:
    import factory_self_heal_actions as actions

    thread = factory_thread("b" * 40, start=11, end=14)
    touched = CompareGitHub([{"filename": "app/run.py", "patch": "@@ -12,2 +12,3 @@\n ctx"}])
    assert "changed those lines" in actions.factory_thread_fix(touched, thread, 7, REVIEWED_HEAD)
    assert touched.paths == [f"compare/{'b' * 40}...{REVIEWED_HEAD}"]
    untouched = CompareGitHub([{"filename": "app/run.py", "patch": "@@ -40,2 +40,3 @@\n ctx"}])
    assert actions.factory_thread_fix(untouched, thread, 7, REVIEWED_HEAD) == ""
    assert actions.factory_thread_fix(touched, thread, 7, "b" * 40) == ""
    assert actions.factory_thread_fix(touched, {**thread, "isOutdated": True}, 7, REVIEWED_HEAD)
    greptile = {**thread, "comments": {"nodes": [{**thread["comments"]["nodes"][0],
                                                  "author": {"login": "greptile-apps"}}]}}
    assert actions.factory_thread_fix(touched, greptile, 7, REVIEWED_HEAD) == ""


def test_sweep_resolves_and_records_a_fixed_factory_thread() -> None:
    import factory_self_heal_actions as actions
    import factory_self_heal_rules as rules
    from tests.test_factory_self_heal import Hub, Store, budget, pr_node

    class CompareHub(Hub):
        def api(self, path: str, *args: str, payload: object | None = None) -> Any:
            if path.startswith("compare/"):
                return {"files": [{"filename": "app/run.py", "patch": "@@ -11,1 +11,2 @@\n ctx"}]}
            return super().api(path, *args, payload=payload)

    hub, store = CompareHub(), Store()
    old = factory_thread("a" * 40)
    old["comments"]["nodes"][0]["body"] = old["comments"]["nodes"][0]["body"].replace("pr-7", "pr-44")
    hub.threads = [pr_node(old)]
    [done] = actions.resolve_answered_threads(hub, store, [], budget())
    assert hub.resolved == ["PRRT_aaa"]
    assert "Resolved the factory's finding on app/run.py in #44" in done["text"]
    marker = rules.parse_marker(hub.records[44][0]["body"])
    assert marker is not None and marker["kind"] == rules.THREAD_RESOLVED


def test_a_look_alike_account_cannot_plant_a_factory_finding() -> None:
    planted = factory_thread("b" * 40)
    planted["comments"]["nodes"][0]["author"] = {"login": "github-actions-x", "__typename": "User"}
    assert findings.thread_finding(planted, pr=7) is None
    user = factory_thread("b" * 40)
    user["comments"]["nodes"][0]["author"]["__typename"] = "User"
    assert findings.superseded_threads([user], pr=7, head=REVIEWED_HEAD, verdict="approve") == []


@pytest.mark.parametrize(("stage", "delivered"), [("factory:review", True), ("factory:changes-requested", False)])
def test_review_stage_mission_delivers_the_contract(tmp_path: Path, stage: str, delivered: bool) -> None:
    script = ROOT / ".github/scripts/factory-review-findings.sh"
    controller = ROOT / ".github/scripts/factory-review-controller.py"
    body = (
        f'source "{script}"\n'
        "factory_collect_review_findings() { : > \"$3\"; }\n"
        "factory_prepare_greptile_outcomes_file() { :; }\n"
        "factory_linked_issue_context() { :; }\n"
        f'ASSIGNED_PR_STAGE={stage} TRUSTED_REVIEW_CONTROLLER="{controller}" '
        f'factory_review_findings_mission 7 {REVIEWED_HEAD} "{tmp_path / "f.md"}"\n'
    )
    result = subprocess.run(["bash", "-c", body], text=True, capture_output=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (findings.PREFIX in result.stdout and REVIEWED_HEAD in result.stdout) is delivered
    assert "CONTRACT UNAVAILABLE" not in result.stdout
