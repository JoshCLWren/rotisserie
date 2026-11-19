from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONTROLLER_PATH = SCRIPT_DIR / "factory-review-controller.py"
HEAD = "a" * 40
OLD_HEAD = "b" * 40


def load_controller():
    spec = importlib.util.spec_from_file_location("factory_review_controller_thread_gate", CONTROLLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rest_proves_no_current_head_inline_comments_without_graphql(monkeypatch):
    controller = load_controller()
    calls: list[list[str]] = []

    def fake_gh_json(args, **_kwargs):
        calls.append(list(args))
        if "reviews?per_page=100" in args[-1]:
            return [[]]
        if "comments?per_page=100" in args[-1]:
            return [[]]
        raise AssertionError(f"unexpected GraphQL call: {args}")

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)
    result = controller.current_head_review_gate(1507, HEAD)

    assert result == {
        "decision": "pass",
        "reason": "no current-head review thread blockers",
    }
    assert not any("graphql" in call for call in calls)


def test_old_head_inline_comments_do_not_require_graphql(monkeypatch):
    controller = load_controller()
    calls: list[list[str]] = []

    def fake_gh_json(args, **_kwargs):
        calls.append(list(args))
        if "reviews?per_page=100" in args[-1]:
            return [[]]
        if "comments?per_page=100" in args[-1]:
            return [[{"commit_id": OLD_HEAD}]]
        raise AssertionError(f"unexpected GraphQL call: {args}")

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)
    result = controller.current_head_review_gate(1507, HEAD)

    assert result["decision"] == "pass"
    assert not any("graphql" in call for call in calls)


def test_current_head_inline_comment_still_requires_thread_resolution(monkeypatch):
    controller = load_controller()
    calls: list[list[str]] = []

    def fake_gh_json(args, **_kwargs):
        calls.append(list(args))
        if "reviews?per_page=100" in args[-1]:
            return [[]]
        if "comments?per_page=100" in args[-1]:
            return [[{"commit_id": HEAD}]]
        if "graphql" in args:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [{"commit": {"oid": HEAD}}],
                                            "pageInfo": {"hasNextPage": False},
                                        },
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                }
            }
        raise AssertionError(f"unexpected call: {args}")

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)
    result = controller.current_head_review_gate(1507, HEAD)

    assert result == {
        "decision": "deny",
        "reason": "current head has an unresolved review thread",
    }
    assert any("graphql" in call for call in calls)


def test_missing_review_comment_commit_id_fails_closed_to_graphql(monkeypatch):
    controller = load_controller()
    calls: list[list[str]] = []

    def fake_gh_json(args, **_kwargs):
        calls.append(list(args))
        if "reviews?per_page=100" in args[-1]:
            return [[]]
        if "comments?per_page=100" in args[-1]:
            return [[{"commit_id": None}]]
        if "graphql" in args:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                }
            }
        raise AssertionError(f"unexpected call: {args}")

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)
    result = controller.current_head_review_gate(1507, HEAD)

    assert result["decision"] == "pass"
    assert any("graphql" in call for call in calls)


def test_quota_only_greptile_review_does_not_block_current_head(monkeypatch):
    """Credits are optional; a current-head quota notice is not a code finding."""
    controller = load_controller()

    def fake_gh_json(args, **_kwargs):
        if "reviews?per_page=100" in args[-1]:
            return [[{
                "user": {"login": "greptile-apps[bot]"},
                "state": "COMMENTED",
                "commit_id": HEAD,
                "body": "JoshCLWren has reached the 50-credit limit for trial accounts.",
            }]]
        if "comments?per_page=100" in args[-1]:
            return [[]]
        raise AssertionError(f"unexpected API call: {args}")

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)
    assert controller.current_head_review_gate(1507, HEAD)["decision"] == "pass"
    assert controller.interpret_required_checks(
        [{"name": "CI Summary", "state": "SUCCESS"}], command_status=0, stderr=""
    )["decision"] == "pass"
    assert controller.interpret_required_checks(
        [{"name": "CI Summary", "state": "FAILURE"}], command_status=1, stderr=""
    )["decision"] == "deny"
