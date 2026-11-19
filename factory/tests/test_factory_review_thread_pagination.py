"""Fail-closed pagination for every factory review-thread lookup.

Covers the worker findings helper (threads and comments nested in each thread)
and the exact-head ``current_head_review_blockers`` merge gate in every
wrapper. A truncated listing must never look like "no unresolved threads".
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"
HELPER = SCRIPTS / "factory-review-findings.sh"
HEAD = "a" * 40
WRAPPERS = (
    "omniroute-factory-worker.sh",
    "nvidia-factory-worker.sh",
    "free-model-factory-worker-primitives.sh",
)


class PageInfo(TypedDict):
    hasNextPage: bool
    endCursor: str | None


class Author(TypedDict):
    login: str


class Comment(TypedDict):
    author: Author
    body: str
    url: str | None


def comment(login: str, body: str) -> Comment:
    return {"author": {"login": login}, "body": body, "url": None}


def threads_doc(nodes: list[dict[str, object]], page_info: PageInfo | None) -> str:
    connection: dict[str, object] = {"nodes": nodes}
    if page_info is not None:
        connection["pageInfo"] = page_info
    return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": connection}}}})


def comments_doc(nodes: list[Comment], page_info: PageInfo) -> str:
    return json.dumps({"data": {"node": {"comments": {"pageInfo": page_info, "nodes": nodes}}}})


def install_fake_gh(tmp_path: Path, responses: dict[str, str | None]) -> dict[str, str]:
    """Fake gh keyed by query kind; a ``None`` response simulates a gh failure.

    Kinds: ``count`` (unresolved-thread count query), ``threads`` (findings
    thread query), ``node`` (per-thread comment pages), ``reviews``.
    """
    data = tmp_path / "fake"
    data.mkdir()
    for kind, body in responses.items():
        if body is not None:
            (data / kind).write_text(body)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {tmp_path}/gh-calls.log
case "$*" in
  *"node(id:"*) kind=node ;;
  *"nodes{{isResolved"*) kind=count ;;
  *graphql*) kind=threads ;;
  *pulls/*/reviews*) kind=reviews ;;
  "pr view"*) printf '' ; exit 0 ;;
  *check-runs*) echo '{{"check_runs":[]}}' ; exit 0 ;;
  *issues/*/comments*) echo '[]' ; exit 0 ;;
  *) exit 1 ;;
esac
[[ -f {data}/$kind ]] || {{ echo "fake gh: $kind failed" >&2; exit 1; }}
cat {data}/$kind
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "JoshCLWren/comic-pile",
    }


def run_bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"set -Eeuo pipefail; source {HELPER!s}; {script}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **env},
    )


DONE: PageInfo = {"hasNextPage": False, "endCursor": None}
MORE: PageInfo = {"hasNextPage": True, "endCursor": "c1"}


def count(tmp_path: Path, pages: str | None) -> subprocess.CompletedProcess[str]:
    env = install_fake_gh(tmp_path, {"count": pages})
    return run_bash("factory_count_unresolved_review_threads 42", env)


def test_count_sums_unresolved_threads_across_every_page(tmp_path: Path) -> None:
    pages = "\n".join(
        [
            threads_doc([{"isResolved": True}] * 100, MORE),
            threads_doc([{"isResolved": False}, {"isResolved": True}], DONE),
        ]
    )
    result = count(tmp_path, pages)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"
    calls = (tmp_path / "gh-calls.log").read_text()
    assert "--paginate" in calls and "$endCursor" in calls


def test_count_fails_closed_when_truncated_missing_or_invalid(tmp_path: Path) -> None:
    cases = {
        "last-page-has-next": threads_doc([{"isResolved": True}], MORE),
        "missing-page-info": threads_doc([{"isResolved": True}], None),
        "invalid-nodes": json.dumps(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": None}}}}}
        ),
        "empty": "",
        "gh-failure": None,
    }
    for name, pages in cases.items():
        case_dir = tmp_path / name
        case_dir.mkdir()
        result = count(case_dir, pages)
        assert result.returncode != 0, name
        assert result.stdout.strip() == "", name


def test_count_treats_missing_resolution_state_as_unresolved(tmp_path: Path) -> None:
    result = count(tmp_path, threads_doc([{"isResolved": None}, {}], DONE))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"


def blockers_script(wrapper: str) -> str:
    text = (SCRIPTS / wrapper).read_text()
    match = re.search(r"(?ms)^current_head_review_blockers\(\) \{\n.*?^\}$", text)
    assert match is not None, wrapper
    return match.group(0)


def run_blockers(tmp_path: Path, wrapper: str, pages: str | None) -> int:
    env = install_fake_gh(tmp_path, {"count": pages, "reviews": "[]"})
    script = f"{blockers_script(wrapper)}\nGITHUB_REPOSITORY=JoshCLWren/comic-pile\n"
    script += f"if current_head_review_blockers 42 {HEAD}; then exit 0; else exit 1; fi"
    return run_bash(script, env).returncode


def test_every_wrapper_merge_gate_pages_threads_and_fails_closed(tmp_path: Path) -> None:
    page_two_open = "\n".join(
        [
            threads_doc([{"isResolved": True}] * 100, MORE),
            threads_doc([{"isResolved": False}], DONE),
        ]
    )
    all_resolved = "\n".join(
        [
            threads_doc([{"isResolved": True}] * 100, MORE),
            threads_doc([{"isResolved": True}], DONE),
        ]
    )
    truncated = threads_doc([{"isResolved": True}], MORE)
    for wrapper in WRAPPERS:
        base = tmp_path / wrapper
        base.mkdir()
        for name, pages, expected in (
            ("page-two-open", page_two_open, 1),
            ("all-resolved", all_resolved, 0),
            ("truncated", truncated, 1),
            ("gh-failure", None, 1),
        ):
            case_dir = base / name
            case_dir.mkdir()
            assert run_blockers(case_dir, wrapper, pages) == expected, (wrapper, name)


def test_no_wrapper_keeps_a_single_page_thread_query() -> None:
    for wrapper in WRAPPERS:
        body = blockers_script(wrapper)
        assert "factory_count_unresolved_review_threads" in body, wrapper
        assert "reviewThreads(first:100){" not in body, wrapper


def test_thread_queries_put_thread_page_info_before_nested_comments() -> None:
    # gh --paginate follows the first pageInfo in each response.
    text = HELPER.read_text()
    for name in ("FACTORY_REVIEW_THREADS_QUERY", "FACTORY_UNRESOLVED_THREADS_QUERY"):
        query = re.search(rf"^{name}='([^']*)'", text, re.M)
        assert query is not None, name
        value = query.group(1)
        assert "reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor}" in value
        assert value.index("pageInfo") < value.index("nodes"), name


def thread_with_more_comments(cursor: str | None) -> dict[str, object]:
    return {
        "id": "PRRT_big",
        "isResolved": False,
        "isOutdated": False,
        "path": "app/x.py",
        "line": 3,
        "originalLine": 3,
        "comments": {
            "totalCount": 3,
            "pageInfo": {"hasNextPage": True, "endCursor": cursor},
            "nodes": [comment("greptile-apps", "big greptile finding")],
        },
    }


def collect(tmp_path: Path, responses: dict[str, str | None]) -> str:
    env = install_fake_gh(tmp_path, {"reviews": "[]", **responses})
    out = tmp_path / "findings.md"
    result = run_bash(f"factory_collect_review_findings 42 {HEAD} {out}", env)
    assert result.returncode == 0, result.stderr
    return out.read_text()


def test_collect_pages_comments_nested_in_each_thread(tmp_path: Path) -> None:
    text = collect(
        tmp_path,
        {
            "threads": threads_doc([thread_with_more_comments("c1")], DONE),
            "node": "\n".join(
                [
                    comments_doc([comment("JoshCLWren", "second page reply")], MORE),
                    comments_doc([comment("JoshCLWren", "third page reply")], DONE),
                ]
            ),
        },
    )
    assert "HIGH PRIORITY G1. app/x.py:3" in text
    assert "- JoshCLWren: second page reply" in text
    assert "- JoshCLWren: third page reply" in text
    assert "INCOMPLETE" not in text
    assert "more comment(s) on this thread not shown" not in text
    calls = (tmp_path / "gh-calls.log").read_text()
    assert "-F id=PRRT_big -F endCursor=c1" in calls


def test_collect_marks_threads_incomplete_when_comment_pages_fail(tmp_path: Path) -> None:
    for name, cursor, node in (
        ("gh-failure", "c1", None),
        ("still-truncated", "c1", comments_doc([comment("x", "partial")], MORE)),
        ("missing-cursor", None, comments_doc([comment("x", "never fetched")], DONE)),
    ):
        case_dir = tmp_path / name
        case_dir.mkdir()
        text = collect(
            case_dir,
            {"threads": threads_doc([thread_with_more_comments(cursor)], DONE), "node": node},
        )
        assert "[INCOMPLETE: not every comment on this thread could be fetched" in text, name
        assert "WARNING: INCOMPLETE REVIEW DATA" in text, name
        assert "thread comments" in text, name
        assert "Do not end with FACTORY_GATE_READY" in text, name
        assert "never fetched" not in text, name


def test_collect_flags_truncated_thread_pages(tmp_path: Path) -> None:
    for name, threads in (
        ("last-page-has-next", threads_doc([thread_with_more_comments(None)], MORE)),
        ("missing-page-info", threads_doc([], None)),
        ("gh-failure", None),
    ):
        case_dir = tmp_path / name
        case_dir.mkdir()
        text = collect(case_dir, {"threads": threads, "node": comments_doc([], DONE)})
        assert "INCOMPLETE REVIEW DATA" in text, name
        assert "Do not end with FACTORY_GATE_READY" in text, name


def test_retired_coderabbit_thread_does_not_block_any_wrapper(tmp_path: Path) -> None:
    nodes = [
        {"isResolved": False, "comments": {"nodes": [{"author": {"login": "coderabbitai[bot]"}}]}},
        {"isResolved": False, "comments": {"nodes": [{"author": {"login": "human-reviewer"}}]}},
    ]
    response = threads_doc(nodes, DONE)
    result = count(tmp_path, response)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"
    only_rabbit = threads_doc(nodes[:1], DONE)
    for wrapper in WRAPPERS:
        case_dir = tmp_path / wrapper
        case_dir.mkdir()
        assert run_blockers(case_dir, wrapper, only_rabbit) == 0, wrapper


