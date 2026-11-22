"""`rotisserie factory checks --misrouted`: review-stage PRs whose own checks are red."""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from rotisserie import cli
from rotisserie.factory_console import commands
from rotisserie.factory_console.github import JSON, GitHub, GitHubError, Obj
from rotisserie.factory_console.service import baseline, checks, doctor, misrouted
from rotisserie.factory_console.service import sync as leases
from rotisserie.factory_console.tracker import Clock

REPO = "acme/widgets"
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
CLIPPY = "error: unused variable: `x`"


def head(pr: int) -> str:
    return f"{pr:040d}"


def row(pr: int, stage: str = "factory:review", owner: str = "factory:unowned") -> Obj:
    return {
        "number": pr,
        "title": f"Fix #{pr}",
        "body": "",
        "headRefName": f"factory/1-{pr}-x",
        "headRefOid": head(pr),
        "labels": [{"name": name} for name in ("factory", stage, owner)],
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "UNSTABLE",
    }


@dataclass
class Fake:
    """Routes ``gh`` calls to scripted responses (regex on the API path) and records them."""

    routes: dict[str, JSON | Callable[[], JSON]] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)
    pulls: list[JSON] = field(default_factory=list)
    variables: list[JSON] = field(default_factory=list)
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)

    def __call__(self, args: Sequence[str], stdin: str | None) -> str:
        argv = list(args)
        self.calls.append((argv, stdin))
        if argv[:2] == ["pr", "list"]:
            return json.dumps(self.pulls)
        if argv[:2] == ["variable", "list"]:
            return json.dumps(self.variables)
        path = next(item for item in argv[1:] if "/" in item)
        if "--allow-escape-sequences" in argv:
            return next((text for key, text in self.texts.items() if path.endswith(key)), "")
        for pattern, value in self.routes.items():
            if re.search(pattern, path):
                found = value() if callable(value) else value
                return json.dumps(found)
        return json.dumps({})

    def puts(self) -> list[tuple[list[str], str | None]]:
        return [(argv, stdin) for argv, stdin in self.calls if "PUT" in argv]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    gh = Fake()
    monkeypatch.delenv("FACTORY_STATE_DSN_FILE", raising=False)
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None: gh(args, stdin))
    gh.routes[f"repos/{REPO}$"] = {"default_branch": "main"}
    return gh


def red(
    fake: Fake,
    pr: int,
    *,
    name: str = "rust-roll",
    log: str = CLIPPY,
    main: str = "success",
    extra: list[JSON] | None = None,
) -> None:
    """PR ``pr`` fails ``name``; main's latest run of the same job concluded ``main``."""
    run, job = 4000 + pr, 5000 + pr
    fake.routes[rf"pulls/{pr}$"] = {
        "number": pr,
        "state": "open",
        "head": {"sha": head(pr)},
        "labels": row(pr)["labels"],
    }
    fake.routes[rf"pulls/{pr}/files"] = [{"filename": f"src/pr{pr}.rs"}]
    fake.routes[rf"commits/{head(pr)}/check-runs"] = {
        "check_runs": [
            {
                "id": job,
                "name": name,
                "conclusion": "failure",
                "html_url": f"https://github.com/{REPO}/actions/runs/{run}/job/{job}",
                "completed_at": "2026-11-22T10:00:00Z",
            },
            {"id": 1, "name": "CI Summary", "conclusion": "success"},
            *(extra or []),
        ]
    }
    fake.texts[f"actions/jobs/{job}/logs"] = log
    fake.routes[rf"actions/runs/{run}$"] = {"workflow_id": 77}
    fake.routes[r"actions/workflows/77/runs"] = {
        "workflow_runs": [
            {"id": 8, "event": "pull_request"},
            {"id": 9, "event": "push", "head_sha": "c" * 40},
        ]
    }
    fake.routes[r"actions/runs/9/jobs"] = {
        "jobs": [
            {
                "id": 90,
                "name": name,
                "conclusion": main,
                "completed_at": "2026-11-22T11:00:00Z",
            }
        ]
    }
    fake.texts["actions/jobs/90/logs"] = log


def green(fake: Fake, pr: int) -> None:
    fake.routes[rf"pulls/{pr}$"] = {"number": pr, "state": "open", "head": {"sha": head(pr)}}
    fake.routes[rf"commits/{head(pr)}/check-runs"] = {
        "check_runs": [{"id": 1, "name": "CI Summary", "conclusion": "success"}]
    }


def found_check(name: str = "rust-roll") -> checks.FailingCheck:
    return checks.FailingCheck(name, "https://x/actions/runs/1/job/2", "compile", CLIPPY)


# scan -----------------------------------------------------------------------------------


def test_scan_keeps_only_failures_the_pr_caused(fake: Fake) -> None:
    fake.pulls = [
        row(1),  # its own Clippy error; main passes rust-roll
        row(3, stage="factory:ci"),  # already in CI repair: not looked at
        row(5, owner="factory:12"),  # its own failure, but a worker holds it
        row(6),  # green
        row(7),  # only an advisory check fails
    ]
    red(fake, 1)
    red(fake, 5)
    green(fake, 6)
    red(fake, 7, name="PR-Agent advisory review")
    fake.variables = [{"name": "FACTORY_ADVISORY_CHECKS", "value": "PR-Agent advisory review, "}]
    found = misrouted.scan(GitHub(REPO))
    assert found.scanned == 4
    assert [(item.pr, item.held) for item in found.misrouted] == [(1, False), (5, True)]
    assert [item.pr for item in found.movable] == [1]
    first = found.misrouted[0]
    assert first.head == head(1) and first.own[0].summary == CLIPPY and not first.inherited
    assert first.text == f"PR #1: in review, but its own checks fail (rust-roll: {CLIPPY})"
    data = found.to_dict()
    assert data["scanned"] == 4 and data["waiting_on_main"] == []
    assert data["misrouted"][0]["held"] is False  # type: ignore[index]
    assert not any(argv[0] == "api" and "PUT" in argv for argv, _ in fake.calls)


def test_a_failure_main_caused_keeps_the_review_path(fake: Fake) -> None:
    fake.pulls = [row(2)]
    red(fake, 2, main="failure")
    found = misrouted.scan(GitHub(REPO), advisory=frozenset())
    assert not found.misrouted and found.waiting_on_main == (2,)


def test_mixed_own_and_inherited_failures_report_both(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.pulls = [row(4)]
    red(fake, 4)
    verdicts = (
        baseline.Verdict(4, "fmt", baseline.INHERITED, "main fails the same way", "diff"),
        baseline.Verdict(4, "rust-roll", baseline.OWN, "the PR's own change", CLIPPY),
    )
    monkeypatch.setattr(baseline, "analyze", lambda _github, _red: baseline.Report((), verdicts))
    monkeypatch.setattr(
        checks, "failing_checks", lambda _github, _number: [found_check("fmt"), found_check()]
    )
    found = misrouted.scan(GitHub(REPO), advisory=frozenset())
    assert found.misrouted[0].inherited == ("fmt",)
    assert [check.name for check in found.misrouted[0].own] == ["rust-roll"]


def test_unreadable_checks_and_main_comparison_never_move_a_pr(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.pulls = [row(1)]
    red(fake, 1)

    def broken(_github: GitHub, _red: object) -> baseline.Report:
        raise GitHubError("rate limited")

    monkeypatch.setattr(baseline, "analyze", broken)
    found = misrouted.scan(GitHub(REPO), advisory=frozenset())
    assert not found.misrouted and found.problems == ("could not compare with main: rate limited",)

    def unreadable(_github: GitHub, _number: int) -> list[checks.FailingCheck]:
        raise GitHubError("gone")

    monkeypatch.setattr(checks, "failing_checks", unreadable)
    assert misrouted.scan(GitHub(REPO), advisory=frozenset()) == misrouted.Scan(scanned=1)


def test_long_failure_lists_are_shortened() -> None:
    item = misrouted.Misrouted(
        1, head(1), "t", "", (found_check("a"), found_check("b"), found_check("c"))
    )
    assert item.text.endswith("(and 1 more))") and not item.held


# requeue --------------------------------------------------------------------------------


def candidate(pr: int = 1, owner: str = "factory:unowned") -> misrouted.Misrouted:
    return misrouted.Misrouted(pr, head(pr), "t", owner, (found_check(),))


def live(labels: list[str], sha: str = head(1), state: str = "open") -> JSON:
    return {
        "number": 1,
        "state": state,
        "head": {"sha": sha},
        "labels": [{"name": name} for name in labels],
    }


def test_requeue_moves_a_free_pr_to_ci_repair_in_one_label_write(fake: Fake) -> None:
    fake.routes[r"pulls/1$"] = live(["factory", "factory:review", "factory:unowned", "bug"])
    moved = misrouted.requeue(GitHub(REPO), candidate())
    assert moved.ok and "moved from review to CI repair" in moved.text
    [(argv, stdin)] = fake.puts()
    assert argv[-3] == f"repos/{REPO}/issues/1/labels"
    assert json.loads(stdin or "") == {
        "labels": ["bug", "factory", "factory:ci", "factory:unowned"]
    }
    assert moved.to_dict()["ok"] is True


@pytest.mark.parametrize(
    ("pull", "says"),
    [
        (live(["factory", "factory:review"], sha="f" * 40), "changed since it was checked"),
        (live(["factory", "factory:review"], state="closed"), "changed since it was checked"),
        (live(["factory", "factory:ci"]), "no longer waiting for review"),
        (live(["factory", "factory:review", "factory:ready"]), "no longer waiting for review"),
        (live(["factory", "factory:review", "factory:42"]), "just claimed by factory:42"),
    ],
)
def test_requeue_refuses_anything_that_changed(fake: Fake, pull: JSON, says: str) -> None:
    fake.routes[r"pulls/1$"] = pull
    moved = misrouted.requeue(GitHub(REPO), candidate())
    assert not moved.ok and says in moved.text
    assert not fake.puts()


def test_requeue_never_touches_a_held_pr(fake: Fake) -> None:
    moved = misrouted.requeue(GitHub(REPO), candidate(owner="factory:12"))
    assert not moved.ok and "held by factory:12" in moved.text
    assert not fake.calls


def test_requeue_respects_a_live_neon_lease(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.routes[r"pulls/1$"] = live(["factory", "factory:review", "factory:unowned"])
    held = leases.LeaseReport(
        "neon",
        (
            leases.LeaseRow("#1", "worker 9 (claim old)", False, ""),
            leases.LeaseRow("#1", "worker 12 (claim abc)", True, ""),
        ),
    )
    monkeypatch.setattr(leases, "lease_report", lambda *_args, **_kwargs: held)
    moved = misrouted.requeue(GitHub(REPO), candidate())
    assert not moved.ok and "live lease (worker 12 (claim abc))" in moved.text
    free = leases.LeaseReport("neon", (leases.LeaseRow("#1", "worker 9", False, ""),))
    monkeypatch.setattr(leases, "lease_report", lambda *_args, **_kwargs: free)
    assert misrouted.requeue(GitHub(REPO), candidate()).ok

    def unreachable(*_args: object, **_kwargs: object) -> leases.LeaseReport:
        raise ConnectionError("neon down")

    monkeypatch.setattr(leases, "lease_report", unreachable)
    moved = misrouted.requeue(GitHub(REPO), candidate())
    assert not moved.ok and "Neon leases could not be read: neon down" in moved.text


def test_requeue_reports_github_errors(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(_github: GitHub, path: str, **_kwargs: object) -> JSON:
        raise GitHubError("not found")

    original = GitHub.repo_api
    monkeypatch.setattr(GitHub, "repo_api", failing)
    moved = misrouted.requeue(GitHub(REPO), candidate())
    assert not moved.ok and "not moved: not found" in moved.text
    monkeypatch.setattr(GitHub, "repo_api", original)
    fake.routes[r"pulls/1$"] = live(["factory", "factory:review", "factory:unowned"])

    def refuse(_github: GitHub, *_args: str, stdin: str | None = None) -> str:
        raise GitHubError("forbidden")

    monkeypatch.setattr(GitHub, "gh", refuse)
    moved = misrouted.requeue(GitHub(REPO), candidate())
    assert not moved.ok and "not moved: forbidden" in moved.text


# CLI ------------------------------------------------------------------------------------


class TTY(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def isatty(self) -> bool:
        return self._tty


def invoke(argv: list[str], *, stdin: str = "", tty: bool = False) -> tuple[int, str]:
    arguments = cli.parser().parse_args(["factory", "--repo", REPO, *argv])
    out = TTY(tty=tty)
    code = commands.main(
        arguments,
        env={"TERM": "xterm"},
        stdout=out,
        stdin=TTY(stdin, tty=tty),
        clock=Clock(now=lambda: NOW, sleep=lambda _seconds: None),
        executable="sh",
    )
    return code, out.getvalue()


def scripted(monkeypatch: pytest.MonkeyPatch, found: misrouted.Scan) -> list[int]:
    moved: list[int] = []

    def requeue(_github: GitHub, item: misrouted.Misrouted) -> misrouted.Moved:
        moved.append(item.pr)
        return misrouted.Moved(item.pr, item.pr != 9, f"PR #{item.pr} result")

    monkeypatch.setattr(misrouted, "scan", lambda _github: found)
    monkeypatch.setattr(misrouted, "requeue", requeue)
    return moved


FOUND = misrouted.Scan(
    (
        misrouted.Misrouted(1, head(1), "t", "factory:unowned", (found_check(),), ("fmt",)),
        misrouted.Misrouted(5, head(5), "t", "factory:12", (found_check(),)),
    ),
    (2,),
    4,
    ("PR #8: its changed files are unknown; failures count as its own",),
)


def test_cli_dry_run_lists_misrouted_prs_and_changes_nothing(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = scripted(monkeypatch, FOUND)
    code, out = invoke(["checks", "--misrouted"])
    assert code == 0 and not moved
    assert "PR #1: in review, but its own checks fail" in out
    assert "Also red because of main: fmt" in out
    assert "Held by factory:12; left alone" in out
    assert "Red only because of main (review stays): #2" in out
    assert "changed files are unknown" in out
    assert "rotisserie factory checks --misrouted --requeue --yes" in out
    code, out = invoke(["--json", "checks", "--misrouted"])
    data = json.loads(out)
    assert [item["pr"] for item in data["scan"]["misrouted"]] == [1, 5]


def test_cli_requeue_asks_first_and_moves_only_free_prs(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = scripted(monkeypatch, FOUND)
    code, out = invoke(["checks", "--misrouted", "--requeue"])
    assert code != 0 and "Add --yes to move these" in out and not moved
    code, out = invoke(["checks", "--misrouted", "--requeue"], stdin="n\n", tty=True)
    assert "Nothing was changed." in out and not moved
    code, out = invoke(["checks", "--misrouted", "--requeue"], stdin="y\n", tty=True)
    assert code == 0 and moved == [1] and "PR #1 result" in out
    code, out = invoke(["--json", "checks", "--misrouted", "--requeue", "--yes"])
    assert moved == [1, 1] and json.loads(out)["requeued"][0]["ok"] is True


def test_cli_requeue_reports_a_refused_move(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    refused = misrouted.Scan((misrouted.Misrouted(9, head(9), "t", "", (found_check(),)),))
    scripted(monkeypatch, refused)
    code, out = invoke(["checks", "--misrouted", "--requeue", "--yes"])
    assert code != 0 and "PR #9 result" in out


def test_cli_with_nothing_misrouted(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    scripted(monkeypatch, misrouted.Scan(scanned=3))
    code, out = invoke(["checks", "--misrouted"])
    assert (
        code == 0 and "No PR waiting for review has failing checks of its own (3 in review)" in out
    )
    assert "--requeue" not in out
    held = misrouted.Scan((FOUND.misrouted[1],))
    scripted(monkeypatch, held)
    code, out = invoke(["checks", "--misrouted", "--requeue", "--yes"])
    assert code != 0 and "Nothing to move: no misrouted PR is free of a worker." in out


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["checks", "pr:1", "--misrouted"], "leave out pr:N"),
        (["checks", "--requeue"], "--requeue works with --misrouted"),
        (["checks"], "checks takes a pull request (pr:N) or --misrouted"),
    ],
)
def test_cli_usage_errors(fake: Fake, argv: list[str], says: str) -> None:
    code, out = invoke(argv)
    assert code == 2 and says in out


# doctor ---------------------------------------------------------------------------------


def test_doctor_reports_and_repairs_misrouted_reviews(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(misrouted, "scan", lambda _github: misrouted.Scan(scanned=2))
    assert doctor.misrouted_reviews(GitHub(REPO)).status == "ok"
    moved = scripted(monkeypatch, FOUND)
    finding = doctor.misrouted_reviews(GitHub(REPO))
    assert finding.status == "warn" and finding.fix == "route-ci-repair"
    assert finding.text == (
        "#1, #5 are waiting for review, but their own checks fail; #5 is held by a worker"
    )
    assert finding.targets == (f"pr:1@{head(1)}",)
    applied = doctor.apply_fixes(GitHub(REPO), [finding])
    assert moved == [1] and [item.fix for item in applied] == ["route-ci-repair"]
    # A PR whose head moved since the finding is not moved by --fix.
    stale = doctor.Finding("misrouted reviews", "warn", "", fix="route-ci-repair", targets=())
    assert doctor.apply_fixes(GitHub(REPO), [stale]) == []
    # A refused move is not reported as applied.
    scripted(monkeypatch, misrouted.Scan((misrouted.Misrouted(9, head(9), "t", "", ()),)))
    nine = doctor.Finding("m", "warn", "", fix="route-ci-repair", targets=(f"pr:9@{head(9)}",))
    assert doctor.apply_fixes(GitHub(REPO), [nine]) == []
    one = misrouted.Scan((FOUND.misrouted[1],))
    scripted(monkeypatch, one)
    single = doctor.misrouted_reviews(GitHub(REPO))
    assert single.text.startswith("#5 is waiting for review, but its own checks fail; #5 is")
    assert not single.fix and not single.targets


def test_advisory_checks_come_from_the_repository_setting(fake: Fake) -> None:
    fake.variables = [{"name": "FACTORY_ADVISORY_CHECKS", "value": " a , ,b"}]
    assert misrouted.advisory_checks(GitHub(REPO)) == frozenset({"a", "b"})
