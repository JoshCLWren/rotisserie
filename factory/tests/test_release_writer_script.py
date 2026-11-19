"""Focused tests for the release-writer command helper."""

import json

from scripts import release_writer


def test_recent_filters_unmerged_and_sorts_by_exact_merge_time(monkeypatch, capsys):
    """Recent reconciliation should use merged_at, not API response order."""
    page = [
        {
            "number": 10,
            "merged_at": "2026-08-12T10:00:00Z",
            "merge_commit_sha": "a" * 40,
            "title": "Older merge",
        },
        {
            "number": 99,
            "merged_at": None,
            "merge_commit_sha": None,
            "title": "Closed without merge",
        },
        {
            "number": 12,
            "merged_at": "2026-08-14T01:00:00Z",
            "merge_commit_sha": "c" * 40,
            "title": "Newest merge",
        },
        {
            "number": 11,
            "merged_at": "2026-08-13T10:00:00Z",
            "merge_commit_sha": "b" * 40,
            "title": "Middle merge",
        },
    ]
    seen_urls: list[str] = []

    def fake_github_request(url: str):
        seen_urls.append(url)
        return page

    monkeypatch.setattr(release_writer, "_github_request", fake_github_request)

    release_writer._recent("JoshCLWren/comic-pile", "2")

    result = json.loads(capsys.readouterr().out)
    assert [item["number"] for item in result] == [12, 11]
    assert len(seen_urls) == 1
    assert "state=closed" in seen_urls[0]
    assert "base=main" in seen_urls[0]


def test_recent_paginates_until_all_closed_prs_are_seen(monkeypatch, capsys):
    """Late pages can contain a newer merge than rows from the first page."""
    first_page = [
        {
            "number": number,
            "merged_at": "2026-01-01T00:00:00Z",
            "merge_commit_sha": f"{number:040d}",
            "title": f"PR {number}",
        }
        for number in range(1, 101)
    ]
    second_page = [
        {
            "number": 101,
            "merged_at": "2026-08-14T01:00:00Z",
            "merge_commit_sha": "f" * 40,
            "title": "Late-page recent merge",
        }
    ]

    def fake_github_request(url: str):
        return second_page if "page=2" in url else first_page

    monkeypatch.setattr(release_writer, "_github_request", fake_github_request)

    release_writer._recent("JoshCLWren/comic-pile", "1")

    result = json.loads(capsys.readouterr().out)
    assert result[0]["number"] == 101


def test_skip_records_hidden_internal_release(monkeypatch, capsys):
    """Internal classifications should become durable without appearing publicly."""
    monkeypatch.setenv("RELEASE_API_URL", "https://example.test/api/v1/releases")
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(method: str, url: str, payload: dict[str, object] | None = None):
        requests.append((method, url, payload))
        return {"id": 42, "visibility": "internal"}

    monkeypatch.setattr(release_writer, "_request", fake_request)

    release_writer._skip(
        json.dumps(
            {
                "source_repository": "JoshCLWren/comic-pile",
                "source_pr_number": 1177,
                "source_merge_sha": "d" * 40,
                "merged_at": "2026-08-14T01:00:00Z",
                "reason": "Factory-only maintenance with no user-facing product change.",
            }
        )
    )

    output = json.loads(capsys.readouterr().out)
    assert output["recorded"] is True
    assert output["skipped"] is True
    assert requests[0][0] == "PUT"
    assert requests[0][1] == "https://example.test/api/v1/releases/"
    payload = requests[0][2]
    assert payload is not None
    assert payload["visibility"] == "internal"
    assert payload["status"] == "published"
    assert payload["source_pr_number"] == 1177
    assert payload["released_at"] == "2026-08-14T01:00:00Z"


def test_pr_emits_trimmed_pull_context(monkeypatch, capsys):
    """PR inspection should emit a bounded context object, not raw API data."""
    seen_urls: list[str] = []

    def fake_github_request(url: str):
        seen_urls.append(url)
        return {
            "number": 1082,
            "title": "Add asynchronous OpenCode release writer",
            "body": "Closes #1070 and #1066.",
            "state": "closed",
            "merged": True,
            "merged_at": "2026-08-11T21:51:56Z",
            "merge_commit_sha": "7d5f47717550357ac5193bcb7caa4bd66a3b48fa",
            "html_url": "https://github.com/JoshCLWren/comic-pile/pull/1082",
            "user": {"login": "JoshCLWren", "other": "ignored"},
            "unwanted": "secret-detail",
        }

    monkeypatch.setattr(release_writer, "_github_request", fake_github_request)

    release_writer._pr("JoshCLWren/comic-pile", "1082")

    result = json.loads(capsys.readouterr().out)
    assert result["number"] == 1082
    assert result["title"] == "Add asynchronous OpenCode release writer"
    assert result["merged"] is True
    assert result["merge_commit_sha"] == "7d5f47717550357ac5193bcb7caa4bd66a3b48fa"
    assert result["author"] == "JoshCLWren"
    assert "unwanted" not in result
    assert seen_urls[0].endswith("/pulls/1082")
    assert seen_urls[0].startswith("https://api.github.com/repos/JoshCLWren/comic-pile")


def test_pr_rejects_bad_input(monkeypatch, capsys):
    """PR inspection should fail on malformed repositories and numbers."""
    import pytest

    called: list[str] = []

    def fake_github_request(url: str):
        called.append(url)
        return {}

    monkeypatch.setattr(release_writer, "_github_request", fake_github_request)

    with pytest.raises(SystemExit) as excinfo:
        release_writer._pr("JoshCLWren", "1")
    assert excinfo.value.code == 2
    assert "repository must use owner/name form" in capsys.readouterr().err

    with pytest.raises(SystemExit) as excinfo:
        release_writer._pr("JoshCLWren/comic-pile", "abc")
    assert excinfo.value.code == 2
    assert "PR number must be an integer" in capsys.readouterr().err

    with pytest.raises(SystemExit) as excinfo:
        release_writer._pr("JoshCLWren/comic-pile", "0")
    assert excinfo.value.code == 2
    assert "PR number must be a positive integer" in capsys.readouterr().err

    assert not called


def test_files_emits_changed_file_summary(monkeypatch, capsys):
    """Changed-file inspection should return bounded per-file summaries."""
    seen_urls: list[str] = []

    def fake_github_request(url: str):
        seen_urls.append(url)
        return [
            {
                "filename": "scripts/release_writer.py",
                "status": "modified",
                "additions": 12,
                "deletions": 3,
                "patch": "ignored-content",
            },
            {"filename": "docs/RELEASE_WRITER.md", "status": "added", "additions": 5, "deletions": 0},
            "not-a-file-object",
        ]

    monkeypatch.setattr(release_writer, "_github_request", fake_github_request)

    release_writer._files("JoshCLWren/comic-pile", "1120")

    result = json.loads(capsys.readouterr().out)
    assert result[0]["filename"] == "scripts/release_writer.py"
    assert result[0]["additions"] == 12
    assert result[0]["status"] == "modified"
    assert "patch" not in result[0]
    assert result[1]["filename"] == "docs/RELEASE_WRITER.md"
    assert len(result) == 2
    assert "per_page=100" in seen_urls[0]
    assert "/pulls/1120/files" in seen_urls[0]


def _issue_responder(
    seen_urls: list[str],
    issues: dict[int, dict[str, object]],
    pull: dict[str, object],
) -> object:
    """Build a GitHub request stub serving one pull request and its issues.

    Args:
        seen_urls: Collector that records every requested URL.
        issues: Issue payloads keyed by issue number.
        pull: Pull request payload returned for any `/pulls/<n>` request.

    Returns:
        A callable suitable for monkeypatching `_github_request`.
    """

    def fake_github_request(url: str, *, missing_ok: bool = False):
        seen_urls.append(url)
        if "/pulls/" in url:
            return pull
        number = int(url.rsplit("/", 1)[-1])
        if number in issues:
            return issues[number]
        if missing_ok:
            return None
        raise AssertionError(f"unexpected issue request: {url}")

    return fake_github_request


def test_issues_extracts_linked_references(monkeypatch, capsys):
    """Linked issue inspection should return full issue context, not bare numbers."""
    seen_urls: list[str] = []
    pull = {
        "number": 1082,
        "title": "Add asynchronous OpenCode release writer",
        "body": "Closes #1070.\nDepends on #1067, part of #1066.",
        "merged_at": "2026-08-11T21:51:56Z",
    }
    issues = {
        1070: {
            "number": 1070,
            "title": "Ship the release ledger",
            "state": "closed",
            "labels": [{"name": "factory"}, {"name": "user-reported"}],
            "body": "Reader-facing What's New surface ships here.",
        },
        1067: {
            "number": 1067,
            "title": "Add rate-limit fallback",
            "state": "closed",
            "labels": [],
            "body": "Internal plumbing only.",
        },
        1066: {
            "number": 1066,
            "title": "Release writer epic",
            "state": "open",
            "labels": "not-a-list",
            "body": "Parent tracking issue.",
        },
    }
    monkeypatch.setattr(
        release_writer,
        "_github_request",
        _issue_responder(seen_urls, issues, pull),
    )

    release_writer._issues("JoshCLWren/comic-pile", "1082")

    result = json.loads(capsys.readouterr().out)
    assert [item["number"] for item in result] == [1066, 1067, 1070]
    assert result[2]["title"] == "Ship the release ledger"
    assert result[2]["state"] == "closed"
    assert result[2]["labels"] == ["factory", "user-reported"]
    assert result[2]["body"] == "Reader-facing What's New surface ships here."
    assert result[0]["labels"] == []
    assert seen_urls[0].endswith("/pulls/1082")
    assert sum(1 for url in seen_urls if "/issues/" in url) == 3


def test_issues_records_parent_and_dependency_relationships(monkeypatch, capsys):
    """Parent, dependency, and follow-up relationships must survive extraction."""
    seen_urls: list[str] = []
    pull = {
        "number": 2910,
        "title": "Adopt Comic Book Library lists",
        "body": "Part of #2128. Depends on #2900. Follow-up UI in #2130.",
    }
    issues: dict[int, dict[str, object]] = {
        number: {"number": number, "title": f"Issue {number}", "state": "open", "body": ""}
        for number in (2128, 2900, 2130)
    }
    monkeypatch.setattr(
        release_writer,
        "_github_request",
        _issue_responder(seen_urls, issues, pull),
    )

    release_writer._issues("JoshCLWren/comic-pile", "2910")

    result = json.loads(capsys.readouterr().out)
    relationships = {item["number"]: item["relationship"] for item in result}
    assert relationships == {2128: "parent", 2900: "dependency", 2130: "follow-up"}


def test_issues_reports_deferred_ui_scope_fences(monkeypatch, capsys):
    """Decisive 'no shipped UI' fences must be visible to a weak model."""
    seen_urls: list[str] = []
    pull = {"number": 2910, "title": "Adopt CBL lists", "body": "Closes #2128."}
    issues = {
        2128: {
            "number": 2128,
            "title": "CBL reading plan support",
            "state": "open",
            "labels": [],
            "body": (
                "No frontend caller changes ship here and no final browser UI is "
                "included. The production UI is out of scope for this issue."
            ),
        }
    }
    monkeypatch.setattr(
        release_writer,
        "_github_request",
        _issue_responder(seen_urls, issues, pull),
    )

    release_writer._issues("JoshCLWren/comic-pile", "2910")

    result = json.loads(capsys.readouterr().out)
    assert result[0]["scope_fences"] == [
        "no frontend callers",
        "no shipped ui",
        "out of scope",
        "ui not shipped",
    ]


def test_issues_marks_unresolvable_references_unavailable(monkeypatch, capsys):
    """A stale reference must not abort context collection."""
    seen_urls: list[str] = []
    pull = {"number": 500, "title": "Work", "body": "Closes #499 and fixes #1070."}
    issues = {
        1070: {"number": 1070, "title": "Live issue", "state": "closed", "body": "ok"}
    }
    monkeypatch.setattr(
        release_writer,
        "_github_request",
        _issue_responder(seen_urls, issues, pull),
    )

    release_writer._issues("JoshCLWren/comic-pile", "500")

    result = json.loads(capsys.readouterr().out)
    assert result[0] == {"number": 499, "unavailable": True}
    assert result[1]["title"] == "Live issue"


def test_issues_bounds_long_issue_bodies(monkeypatch, capsys):
    """Oversized issue bodies must be truncated instead of flooding the prompt."""
    seen_urls: list[str] = []
    body = "x" * (release_writer._MAX_ISSUE_BODY + 500)
    pull = {"number": 600, "title": "Work", "body": "Closes #601."}
    issues = {601: {"number": 601, "title": "Long", "state": "open", "body": body}}
    monkeypatch.setattr(
        release_writer,
        "_github_request",
        _issue_responder(seen_urls, issues, pull),
    )

    release_writer._issues("JoshCLWren/comic-pile", "600")

    result = json.loads(capsys.readouterr().out)
    assert len(result[0]["body"]) == release_writer._MAX_ISSUE_BODY
    assert result[0]["body_truncated"] is True


def test_issues_skips_own_pr_number(monkeypatch, capsys):
    """A PR referencing its own number must not treat itself as a linked issue."""
    seen_urls: list[str] = []
    pull = {"number": 42, "title": "PR #42 work", "body": "Self reference #42 only."}
    monkeypatch.setattr(
        release_writer, "_github_request", _issue_responder(seen_urls, {}, pull)
    )

    release_writer._issues("JoshCLWren/comic-pile", "42")

    result = json.loads(capsys.readouterr().out)
    assert result == []


def test_issues_excludes_ordinal_markers(monkeypatch, capsys):
    """Ordinal/step markers like 'Step #3' must not be extracted as linked issues."""
    seen_urls: list[str] = []
    pull = {
        "number": 100,
        "title": "Migration",
        "body": "Step #3 of the guide. Build #4 is deployable.",
    }
    monkeypatch.setattr(
        release_writer, "_github_request", _issue_responder(seen_urls, {}, pull)
    )

    release_writer._issues("JoshCLWren/comic-pile", "100")

    result = json.loads(capsys.readouterr().out)
    assert result == []


def test_issues_keeps_real_references_after_ordinal_markers(monkeypatch, capsys):
    """Legitimate issue references must survive filtering of ordinal markers."""
    seen_urls: list[str] = []
    pull = {
        "number": 100,
        "title": "Migration phase #2",
        "body": "Step #3 of the guide. Closes #1070. Build #4 is deployable.",
    }
    issues = {1070: {"number": 1070, "title": "Real", "state": "open", "body": ""}}
    monkeypatch.setattr(
        release_writer, "_github_request", _issue_responder(seen_urls, issues, pull)
    )

    release_writer._issues("JoshCLWren/comic-pile", "100")

    result = json.loads(capsys.readouterr().out)
    assert [item["number"] for item in result] == [1070]


def test_issues_excludes_version_step_markers(monkeypatch, capsys):
    """Version-step markers like 'v1.2 #3' and 'v2 #4' must not be linked issues."""
    seen_urls: list[str] = []
    pull = {
        "number": 100,
        "title": "Release v1.2 #3 ships today.",
        "body": "In v2 #4 the API changed. Build 1.1 #5 passed.",
    }
    monkeypatch.setattr(
        release_writer, "_github_request", _issue_responder(seen_urls, {}, pull)
    )

    release_writer._issues("JoshCLWren/comic-pile", "100")

    result = json.loads(capsys.readouterr().out)
    assert result == []


def test_issues_keeps_real_references_after_version_step_markers(monkeypatch, capsys):
    """Real issue references must survive filtering of version-step markers."""
    seen_urls: list[str] = []
    pull = {
        "number": 100,
        "title": "Release v1.2 #3 ships today.",
        "body": "In v2 #4 the API changed. Fixes #1070.",
    }
    issues = {1070: {"number": 1070, "title": "Real", "state": "open", "body": ""}}
    monkeypatch.setattr(
        release_writer, "_github_request", _issue_responder(seen_urls, issues, pull)
    )

    release_writer._issues("JoshCLWren/comic-pile", "100")

    result = json.loads(capsys.readouterr().out)
    assert [item["number"] for item in result] == [1070]


def test_issues_excludes_parenthesized_version_step_markers(monkeypatch, capsys):
    """Assert parenthesised or bracket-quoted markers are excluded.

    Parenthesized or bracket-quoted version-step patterns must not be linked
    issues, while real references in the same text are preserved.
    """
    seen_urls: list[str] = []
    pull = {
        "number": 100,
        "title": "Release (v1.2) #3 ships today.",
        "body": "In [v1] #5 the API changed. Step (v1.1) #6 follows. Fixes #1070.",
    }
    issues = {1070: {"number": 1070, "title": "Real", "state": "open", "body": ""}}
    monkeypatch.setattr(
        release_writer, "_github_request", _issue_responder(seen_urls, issues, pull)
    )

    release_writer._issues("JoshCLWren/comic-pile", "100")

    result = json.loads(capsys.readouterr().out)
    assert [item["number"] for item in result] == [1070]


def test_issues_excludes_unbalanced_parenthesized_markers(monkeypatch, capsys):
    """Unbalanced (vN #M and [vN #M must not produce false issue refs."""
    seen_urls: list[str] = []
    pull = {
        "number": 100,
        "title": "Release (v2) #3 ships today.",
        "body": "In (v2 #3 the API changed. [v4 #5 broke. Fixes #1070.",
    }
    issues = {1070: {"number": 1070, "title": "Real", "state": "open", "body": ""}}
    monkeypatch.setattr(
        release_writer, "_github_request", _issue_responder(seen_urls, issues, pull)
    )

    release_writer._issues("JoshCLWren/comic-pile", "100")
    result = json.loads(capsys.readouterr().out)
    assert [item["number"] for item in result] == [1070]

