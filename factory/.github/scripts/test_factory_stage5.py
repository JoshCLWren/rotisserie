#!/usr/bin/env python3
"""Network-free regression coverage for factory repair stage 5."""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_factory_work_policy = importlib.import_module("factory_work_policy")
FACTORY_NO_DIFF_RETRY_LIMIT = _factory_work_policy.FACTORY_NO_DIFF_RETRY_LIMIT
FACTORY_NO_DIFF_RETRY_RESET_SECONDS = _factory_work_policy.FACTORY_NO_DIFF_RETRY_RESET_SECONDS
TRUSTED_FACTORY_APP_SLUGS = _factory_work_policy.TRUSTED_FACTORY_APP_SLUGS
build_candidates = _factory_work_policy.build_candidates
comment_is_trusted = _factory_work_policy.comment_is_trusted
lease_is_stale = _factory_work_policy.lease_is_stale
no_diff_attempts_from_comments = _factory_work_policy.no_diff_attempts_from_comments


def load_controller():
    spec = importlib.util.spec_from_file_location(
        "factory_work_controller_stage5", SCRIPTS / "factory-work-controller.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def labels(*names: str) -> list[dict[str, str]]:
    return [{"name": name} for name in names]


def issue(number: int, *extra_labels: str) -> dict[str, object]:
    return {
        "number": number,
        "state": "OPEN",
        "title": f"Issue {number}",
        "labels": labels("factory", "factory:unowned", *extra_labels),
        "createdAt": "2026-08-18T00:00:00Z",
    }


def comment(body: str, association: str = "OWNER") -> dict[str, str]:
    return {"body": body, "author_association": association}


def no_diff_marker(
    issue_number: int, epoch: int, worker: str = "opencode-nvidia-factory-6"
) -> str:
    return (
        "<!-- comic-pile-factory-claim-released-v3:"
        f"issue-{issue_number}:{worker}:{epoch}:no-persisted-change-handoff -->"
    )


def test_three_digit_workers_require_canonical_roster_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New roster workers work, but unlisted or malformed IDs cannot claim."""
    controller = load_controller()
    monkeypatch.setattr(
        controller,
        "worker_sources",
        lambda: {"11": "nvidia", "104": "nvidia", "107": "nvidia"},
    )
    for worker in ("11", "104", "107"):
        assert controller.is_authorized_fixed_worker(worker)
    for worker in ("5", "108", "999", "0104", "104x", "0", ""):
        assert not controller.is_authorized_fixed_worker(worker)


def test_retry_budget_exhausts_at_limit() -> None:
    target = issue(31)
    below = build_candidates(
        [target],
        [],
        no_diff_attempts_by_issue={31: FACTORY_NO_DIFF_RETRY_LIMIT - 1},
    )
    exhausted = build_candidates(
        [target],
        [],
        no_diff_attempts_by_issue={31: FACTORY_NO_DIFF_RETRY_LIMIT},
    )
    assert any(candidate.number == 31 for candidate in below)
    assert not any(candidate.number == 31 for candidate in exhausted)


def test_retry_counter_is_rolling_and_ages_out() -> None:
    now = 2_000_000_000
    recent = now - 60
    expired = now - FACTORY_NO_DIFF_RETRY_RESET_SECONDS - 1
    comments = [
        comment(no_diff_marker(31, expired)),
        comment(no_diff_marker(31, recent)),
        comment(no_diff_marker(31, recent - 1, "opencode-free-model-factory-17")),
    ]
    counts = no_diff_attempts_from_comments(comments, now_epoch=now)
    assert counts == {31: 2}

    all_expired = [
        comment(
            no_diff_marker(
                31,
                now - FACTORY_NO_DIFF_RETRY_RESET_SECONDS - offset - 1,
            )
        )
        for offset in range(FACTORY_NO_DIFF_RETRY_LIMIT)
    ]
    assert no_diff_attempts_from_comments(all_expired, now_epoch=now) == {}


def test_untrusted_comment_cannot_consume_retry_budget() -> None:
    now = 2_000_000_000
    counts = no_diff_attempts_from_comments(
        [comment(no_diff_marker(31, now - 1), association="NONE")],
        now_epoch=now,
    )
    assert counts == {}


def test_github_actions_app_comment_is_trusted_without_trusting_all_contributors() -> None:
    actions_comment = {
        "body": "marker",
        "author_association": "CONTRIBUTOR",
        "performed_via_github_app": {"slug": "github-actions"},
    }
    arbitrary_contributor = {
        "body": "marker",
        "author_association": "CONTRIBUTOR",
    }
    assert comment_is_trusted(actions_comment) is True
    assert comment_is_trusted(arbitrary_contributor) is False


def test_worker_github_app_is_not_a_trusted_marker_author() -> None:
    worker_app = {
        "body": "marker",
        "author_association": "OWNER",
        "performed_via_github_app": {"slug": "mark-cordova"},
    }
    assert TRUSTED_FACTORY_APP_SLUGS == {"github-actions"}
    assert comment_is_trusted(worker_app) is False


def test_github_actions_no_diff_marker_counts_toward_budget() -> None:
    now = 2_000_000_000
    actions_comment = {
        "body": no_diff_marker(31, now - 1),
        "author_association": "CONTRIBUTOR",
        "performed_via_github_app": {"slug": "github-actions"},
    }
    assert no_diff_attempts_from_comments([actions_comment], now_epoch=now) == {31: 1}


def test_action_required_is_retry_later_not_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = load_controller()
    monkeypatch.setattr(
        controller,
        "run_gh",
        lambda *args, **kwargs: json.dumps([{"state": "ACTION_REQUIRED"}]),
    )
    assert controller.required_checks_failed(123) is False


def test_stage4_hard_failure_states_still_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = load_controller()
    monkeypatch.setattr(
        controller,
        "run_gh",
        lambda *args, **kwargs: json.dumps([{"state": "STALE"}]),
    )
    assert controller.required_checks_failed(123) is True


def test_active_run_resolution_degrades_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = load_controller()

    def fake_gh_json(args, **kwargs):
        joined = " ".join(args)
        if "status=queued" in joined:
            return [
                {
                    "workflow_runs": [
                        {"id": 111, "display_title": "Factory 7 · queued"},
                        {"id": 222, "display_title": "Free Model Factory Entry"},
                    ]
                }
            ]
        if "status=in_progress" in joined:
            return [{"workflow_runs": []}]
        if "issues/1093/comments" in joined:
            return [[]]
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)
    workers, unresolved = controller.active_fixed_workers()
    assert workers == {7}
    assert unresolved == {"222"}


def test_github_actions_heartbeat_resolves_unlabeled_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = load_controller()

    def fake_gh_json(args, **kwargs):
        joined = " ".join(args)
        if "status=queued" in joined:
            return [
                {
                    "workflow_runs": [
                        {"id": 222, "display_title": "Free Model Factory Entry"}
                    ]
                }
            ]
        if "status=in_progress" in joined:
            return [{"workflow_runs": []}]
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)
    # Exact run attribution is durable, no longer sourced from issue comments.
    import factory_runtime_view

    monkeypatch.setattr(factory_runtime_view, "run_workers", lambda _repo, _runs: {"222": "8"})
    monkeypatch.setattr(controller, "is_authorized_fixed_worker", lambda worker: worker == "8")
    workers, unresolved = controller.active_fixed_workers()
    assert workers == {8}
    assert unresolved == set()


def test_unresolvable_run_holds_fixed_leases_but_local_gc_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = load_controller()
    now = 2_000_000_000
    released: list[tuple[int, str]] = []
    monkeypatch.setattr(controller, "active_fixed_workers", lambda: ({7}, {"222"}))
    monkeypatch.setattr(controller, "renew_verified_workers", lambda *_args: 0)
    targets = [
        {
            "number": number,
            "state": "OPEN",
            "updatedAt": "2020-01-01T00:00:00Z",
            "body": "",
            "labels": labels("factory", owner),
        }
        for number, owner in (
            (1, "factory:7"), (2, "factory:8"), (3, "factory:local")
        )
    ]
    monkeypatch.setattr(controller, "list_issues", lambda: targets)
    monkeypatch.setattr(controller, "list_prs", lambda: [])
    local_ttl = _factory_work_policy.LOCAL_LEASE_TTL_SECONDS
    monkeypatch.setattr(
        controller, "latest_lease_activity_epoch", lambda number: now - (local_ttl + 1)
    )

    def release_only_local(number: int, owner: str, **kwargs: object) -> bool:
        assert number == 3
        assert owner == "factory:unowned"
        assert kwargs["expected_owner"] == "factory:local"
        assert kwargs["observed_at_epoch"] == now
        released.append((number, owner))
        return True

    monkeypatch.setattr(controller, "replace_factory_labels", release_only_local)

    assert controller.reconcile_stale_leases(now_epoch=now) == [3]
    assert released == [(3, "factory:unowned")]


def test_resolved_inactive_fixed_worker_reclaims_only_after_ttl() -> None:
    now = 2_000_000_000
    kwargs = {
        "owner": "factory:8",
        "active_fixed_workers": {7},
        "has_unresolved_active_runs": False,
        "now_epoch": now,
        "fixed_ttl_seconds": 900,
    }
    assert lease_is_stale(latest_activity_epoch=now - 900, **kwargs) is False
    assert lease_is_stale(latest_activity_epoch=now - 901, **kwargs) is True
    assert lease_is_stale(latest_activity_epoch=None, **kwargs) is False


def test_local_lease_reclaims_only_after_24h_ttl() -> None:
    now = 2_000_000_000
    ttl = _factory_work_policy.LOCAL_LEASE_TTL_SECONDS
    kwargs = {
        "owner": "factory:local",
        "active_fixed_workers": set(),
        "has_unresolved_active_runs": False,
        "now_epoch": now,
    }
    assert lease_is_stale(latest_activity_epoch=now - ttl, **kwargs) is False
    assert lease_is_stale(latest_activity_epoch=now - ttl - 1, **kwargs) is True
    assert lease_is_stale(latest_activity_epoch=None, **kwargs) is False


def test_active_or_ambiguous_fixed_worker_is_never_reclaimed() -> None:
    now = 2_000_000_000
    assert (
        lease_is_stale(
            "factory:7",
            active_fixed_workers={7},
            has_unresolved_active_runs=False,
            latest_activity_epoch=now - 10_000,
            now_epoch=now,
            fixed_ttl_seconds=900,
        )
        is False
    )
    assert (
        lease_is_stale(
            "factory:8",
            active_fixed_workers=set(),
            has_unresolved_active_runs=True,
            latest_activity_epoch=now - 10_000,
            now_epoch=now,
            fixed_ttl_seconds=900,
        )
        is False
    )
