"""`checks main`: why a branch's product CI fails, without factory worker noise."""

from __future__ import annotations

from typing import Any, cast

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import checks

TSC_LOG = (
    "2026-11-23T23:20:01.1Z src/unit/ThreadDetailView.test.tsx(746,59): error TS2741: "
    "Property 'evaluator' is missing in type 'X' but required in type 'Y'.\n"
    "2026-11-23T23:20:02.0Z ##[error]Process completed with exit code 1.\n"
)


class Branch:
    repo = "acme/widgets"

    def repo_api(self, route: str) -> Any:
        if route.startswith("actions/runs/55/jobs"):
            return {
                "jobs": [
                    {"id": 1, "name": "Frontend", "conclusion": "failure", "html_url": "j1"},
                    {"id": 2, "name": "Backend", "conclusion": "success", "html_url": "j2"},
                ]
            }
        raise AssertionError(f"unexpected read: {route}")

    def text(self, path: str) -> str:
        assert path == "actions/jobs/1/logs"
        return TSC_LOG


def test_typescript_errors_are_named_with_file_and_line() -> None:
    kind, summary, files = checks.summarize_log(TSC_LOG)
    assert kind == "compile"
    assert summary.startswith("src/unit/ThreadDetailView.test.tsx(746,59): error TS2741:")
    assert files == ("src/unit/ThreadDetailView.test.tsx",)


def test_only_the_named_ci_runs_count_on_a_branch_commit() -> None:
    found = checks.failing_checks_at(cast(GitHub, Branch()), "a" * 40, run_ids=[55])
    assert [(item.name, item.kind) for item in found] == [("Frontend", "compile")]
