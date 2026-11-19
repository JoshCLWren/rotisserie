"""Pinned ComicPile decision interpreter with no host/process/mutation capabilities.

Only audited AST definitions are loaded, never module imports or entrypoints.
The full byte-identical files remain evidence, not executable product modules.
"""

from __future__ import annotations
import __future__

import ast
import builtins
import gzip
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

SOURCE_SHA = "8692c718458282af9300edffce72035145ff6655"
SOURCE_FILES = {
    "factory_main_health.py.txt": (
        "9761cf1e919ec20ed0923fa3b581fbf03002dae3677ffd9e008afe7e5abe4d44"
    ),
    "factory-review-controller.py.txt": (
        "47ff31461e3fea5c5941b8b5dabd1d1527458c553883b928565ee2125eada573"
    ),
    "factory_review_policy.py.txt": (
        "2e4694ef4ca50dab4017a7ede760671e0ab1575977ce879ba789fd66c9cc9ca6"
    ),
    "factory-ready-merge-drain.yml.txt": (
        "fa8c2f5133ba89529c7e248cc5db6a7e2b5e9195c3f085db585f12705c8c6d9f"
    ),
}
POLICY_FUNCTIONS = frozenset(
    {
        "producer_worker_from_pr",
        "parse_review_marker",
        "parse_head_contributor_marker",
        "performed_via_untrusted_app",
        "trusted_comment_bodies",
        "recorded_pr_contributors",
        "current_head_contributors",
        "head_contributor_provenance",
        "current_head_approvers",
        "head_has_authorized_approval",
    }
)
POLICY_CONSTANTS = frozenset(
    {
        "TRUSTED_COMMENT_LOGIN",
        "TRUSTED_COMMENT_ASSOCIATIONS",
        "TRUSTED_MARKER_APP_SLUGS",
        "REVIEW_MARKER_RE",
        "BRANCH_PRODUCER_RE",
        "BODY_PRODUCER_RE",
        "HEAD_CONTRIBUTOR_RE",
    }
)
CONTROLLER_FUNCTIONS = frozenset(
    {
        "gate_result",
        "json_object",
        "json_array",
        "labels_of",
        "linked_issue_from_branch",
        "flatten_pages",
        "review_comment_bodies",
        "interpret_required_checks",
        "required_checks_gate",
        "interpret_review_threads",
        "review_thread_query_required",
        "current_head_review_gate",
        "poll_mergeable_gate",
        "mechanical_merge_gate",
        "authorize_ready",
    }
)
CONTROLLER_CONSTANTS = frozenset(
    {
        "HEAD_RE",
        "MERGEABLE_POLL_ATTEMPTS",
        "MERGEABLE_POLL_INTERVAL_SECONDS",
        "PASSING_CHECK_STATES",
        "RETRY_CHECK_STATES",
        "FAILING_CHECK_STATES",
        "NO_REQUIRED_CHECKS_RE",
        "NO_REQUIRED_CHECKS_REASON",
    }
)


def verified_source(directory: Path, sha: str) -> dict[str, bytes]:
    if sha != SOURCE_SHA:
        raise ValueError("source refresh changed: audit and repin before comparison")
    result = {}
    for name, digest in SOURCE_FILES.items():
        try:
            with gzip.open(directory / f"{name}.gz", "rb") as stream:
                value = stream.read(1024 * 1024 + 1)
        except OSError as error:
            raise ValueError(f"pinned source integrity mismatch: {name}") from error
        if hashlib.sha256(value).hexdigest() != digest:
            raise ValueError(f"pinned source integrity mismatch: {name}")
        result[name] = value
    return result


def _load(
    raw: bytes, functions: frozenset[str], constants: frozenset[str], env: dict[str, Any]
) -> None:
    tree = ast.parse(raw)
    nodes: list[ast.stmt] = []
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
            found.add(node.name)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in constants:
                nodes.append(node)
                found.add(target.id)
    if found != functions | constants:
        raise ValueError("source decision slice is incomplete")
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(
        compile(
            module,
            "<pinned-completion-decisions>",
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        env,
    )


class SourceDecisions:
    """Captured reads only. Unknown calls fail instead of falling through to gh."""

    def __init__(self, files: dict[str, bytes], capture: dict[str, Any]) -> None:
        if set(files) != set(SOURCE_FILES) or any(
            hashlib.sha256(files[name]).hexdigest() != digest
            for name, digest in SOURCE_FILES.items()
        ):
            raise ValueError("source interpreter requires verified pinned bytes")
        self.capture = capture
        self.reads: list[str] = []
        self.suppressed: list[dict[str, object]] = []
        safe = {
            name: getattr(builtins, name)
            for name in (
                "object",
                "str",
                "int",
                "bool",
                "set",
                "list",
                "dict",
                "tuple",
                "frozenset",
                "len",
                "isinstance",
                "all",
                "any",
                "sorted",
                "max",
                "range",
                "enumerate",
                "RuntimeError",
                "ValueError",
            )
        }
        self.env: dict[str, Any] = {
            "__builtins__": safe,
            "re": re,
            "json": json,
            "Mapping": Mapping,
            "Iterable": Iterable,
            "Any": Any,
            "cast": cast,
        }
        _load(files["factory_review_policy.py.txt"], POLICY_FUNCTIONS, POLICY_CONSTANTS, self.env)
        _load(
            files["factory-review-controller.py.txt"],
            CONTROLLER_FUNCTIONS,
            CONTROLLER_CONSTANTS,
            self.env,
        )
        _load(
            files["factory_main_health.py.txt"],
            frozenset({"workflow_runs", "summarize"}),
            frozenset({"FAILING_CONCLUSIONS"}),
            self.env,
        )
        repo = capture["repository"]
        self.env.update(
            REPO=f"{repo['owner']}/{repo['name']}",
            pr_json=self._pr,
            gh_json=self._json,
            run_gh=self._checks,
            target_json=self._issue,
            replace_factory_labels=self._suppress,
            # Exception details contain no external transport output.
            redact_review_text=lambda text: "captured read unavailable",
        )
        poll = self.env["poll_mergeable_gate"]
        # A comparison may inspect this captured view once, never poll a changing host.
        self.env["poll_mergeable_gate"] = lambda number, head: poll(
            number, head, poll_attempts=1, poll_interval=0
        )

    def _pr(self, number: int) -> dict[str, Any]:
        if number != self.capture["selected_pr"]:
            raise RuntimeError("uncaptured PR read")
        self.reads.append("pr")
        return cast(dict[str, Any], self.capture["pr"])

    def _issue(self, number: int) -> dict[str, Any]:
        for issue in self.capture["issues"]:
            if issue["number"] == number:
                self.reads.append("issue")
                return cast(dict[str, Any], issue)
        raise RuntimeError("uncaptured issue read")

    def _suppress(self, number: int, owner: str, stage: str) -> None:
        self.suppressed.append(
            {"kind": "replace_labels", "target": number, "owner": owner, "stage": stage}
        )

    def _checks(self, args: list[str], *, check: bool = True) -> SimpleNamespace:
        del check
        expected = [
            "pr",
            "checks",
            str(self.capture["selected_pr"]),
            "--repo",
            self.env["REPO"],
            "--required",
            "--json",
            "state",
        ]
        if args != expected:
            raise RuntimeError("source attempted an unallowlisted command")
        self.reads.append("checks")
        rows = self.capture["checks"]
        return SimpleNamespace(
            stdout=json.dumps(rows) if rows is not None else "", stderr="", returncode=0
        )

    def _json(self, args: list[str]) -> object:
        number = self.capture["selected_pr"]
        prefix = f"repos/{self.env['REPO']}"
        routes = {
            f"{prefix}/issues/{number}/comments?per_page=100": "comments",
            f"{prefix}/pulls/{number}/reviews?per_page=100": "host_reviews",
            f"{prefix}/pulls/{number}/comments?per_page=100": "inline_comments",
        }
        if args[:3] == ["api", "--paginate", "--slurp"] and len(args) == 4 and args[3] in routes:
            key = routes[args[3]]
            self.reads.append(key)
            value = self.capture[key]
            if value is None:
                raise RuntimeError("captured read unavailable")
            return value
        if args[:2] == ["api", "graphql"] and args[2:8] == [
            "-F",
            f"owner={self.capture['repository']['owner']}",
            "-F",
            f"name={self.capture['repository']['name']}",
            "-F",
            f"number={number}",
        ]:
            self.reads.append("threads")
            value = self.capture["threads"]
            if value is None:
                raise RuntimeError("captured read unavailable")
            return {"data": {"repository": {"pullRequest": {"reviewThreads": value}}}}
        raise RuntimeError("source attempted an unallowlisted read")

    def decide(self) -> dict[str, Any]:
        number, expected = self.capture["selected_pr"], self.capture["expected_head"]
        try:
            health = self.env["summarize"](self.capture["main_runs"], self.capture["main_sha"])
            health_passed = health["failing"] == 0
        except ValueError:
            health, health_passed = None, False
        if not health_passed:
            return {
                "authorization": None,
                "mechanical": None,
                "main_health": health,
                "selected": False,
                "expected_revision": expected,
                "reads": ["main_health"],
                "suppressed_effects": [],
                "merge_invoked": False,
            }
        self.reads.append("main_health")
        try:
            authorization = self.env["authorize_ready"](number)
        except (RuntimeError, TypeError, ValueError, KeyError, AttributeError):
            return {
                "authorization": None,
                "mechanical": None,
                "selected": False,
                "expected_revision": expected,
                "reads": list(self.reads),
                "suppressed_effects": list(self.suppressed),
                "merge_invoked": False,
                "error": "captured_source_read_unavailable",
                "main_health": health,
            }
        mechanical = None
        eligible = authorization["authorized"] and authorization["head"] == expected
        if eligible:
            try:
                mechanical = self.env["mechanical_merge_gate"](number, expected)
            except (TypeError, ValueError, KeyError, AttributeError):
                mechanical = {
                    "decision": "retry",
                    "reason": "captured source payload was ambiguous",
                }
            eligible = mechanical["decision"] == "pass"
        return {
            "main_health": health,
            "authorization": authorization,
            "mechanical": mechanical,
            "selected": bool(eligible),
            "expected_revision": expected,
            "reads": list(self.reads),
            "suppressed_effects": list(self.suppressed),
            "merge_invoked": False,
        }

    def gates(self) -> tuple[dict[str, str], dict[str, str]]:
        """Observe pure mechanical lanes even when source authorization stops early."""
        number, head = self.capture["selected_pr"], self.capture["expected_head"]
        try:
            mergeable = self.env["poll_mergeable_gate"](number, head)
            review = self.env["current_head_review_gate"](number, head)
        except (TypeError, ValueError, KeyError, AttributeError):
            return (
                {"decision": "retry", "reason": "captured source payload was ambiguous"},
                {"decision": "retry", "reason": "captured source payload was ambiguous"},
            )
        return mergeable, review
