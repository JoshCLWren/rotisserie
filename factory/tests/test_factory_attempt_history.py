"""Exercise pass-scoped retry-history reads through the real assignment controller."""

from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"
NOW = 2_000_000_000
HEAD = "a" * 40


def _minute_floor(epoch: int) -> int:
    """Mirror the dispatcher's shared-cache key rounding for a ``since`` boundary."""
    return epoch - epoch % 60


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
HISTORY = _load("factory_attempt_history", SCRIPTS / "factory_attempt_history.py")
CONTROLLER = _load("factory_history_controller_test", SCRIPTS / "factory-work-controller.py")


def _attempt(identity: int, epoch: int) -> dict[str, object]:
    return {
        "id": identity, "author_association": "OWNER",
        "body": (
            "<!-- comic-pile-factory-claim-released-v3:pr-2264:worker:"
            f"{epoch}:review-agent-failed:sha={HEAD}:stage=factory:review:conflicted=0 -->"
        ),
    }


def test_multiple_assignments_read_fifteen_history_pages_once_and_honor_new_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A retry arriving between workers suppresses the next lease without a full rescan."""
    pages = [[{"id": page * 100 + row + 1, "body": "ordinary comment"}
              for row in range(100)] for page in range(15)]
    pages[0][:2] = [_attempt(1, NOW - 10), _attempt(2, NOW - 9)]
    forged = {**_attempt(2001, NOW + 5), "performed_via_github_app": {"slug": "worker-app"}}
    responses = [
        pages,
        [[forged], [_attempt(1501, NOW + 5), _attempt(1502, NOW + 5)]],
        [[{"id": 1501, "body": "withdrawn"}]],
    ]
    since_values: list[int] = []
    page_reads: list[int] = []

    def github(args: list[str]) -> object:
        assert args[:3] == ["api", "--paginate", "--slurp"]
        query = parse_qs(urlsplit(args[-1]).query)
        since_values.append(int(datetime.fromisoformat(query["since"][0]).timestamp()))
        response = responses.pop(0)
        page_reads.append(len(response))
        return response

    pr = {
        "number": 2264, "title": "Product regression", "body": "Closes #2200",
        "headRefName": "factory/50-2200-opencode-free", "headRefOid": HEAD,
        "labels": [{"name": label} for label in ("factory", "factory:unowned", "factory:review")],
        "createdAt": "2026-01-01T00:00:00Z", "isDraft": False,
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
    }
    monkeypatch.setenv("FACTORY_ATTEMPT_HISTORY_CACHE", str(tmp_path / "pass.json"))
    monkeypatch.setattr(CONTROLLER, "gh_json", github)
    # This retry-history fixture explicitly simulates retired worker 48.
    # Production dispatch must still require real roster membership.
    monkeypatch.setattr(CONTROLLER, "worker_sources", lambda: {"46": "kilo-auto", "48": "nvidia", "53": "nvidia"})
    # Simulated NVIDIA workers are credentialed, so a None result for 48 is
    # caused by retry history, not by a missing credential.
    monkeypatch.setenv("FACTORY_HAS_NVIDIA_API_KEY", "true")
    monkeypatch.setattr(CONTROLLER, "quota_pause_for", lambda worker: None)
    monkeypatch.setattr(CONTROLLER, "runtime_quota_block_reason", lambda worker: None)
    monkeypatch.setattr(CONTROLLER, "worker_has_active_lease", lambda worker: False)
    monkeypatch.setattr(CONTROLLER, "budget_block_reason", lambda worker: None)
    monkeypatch.setattr(CONTROLLER, "omniroute_free_entry_capacity", lambda: {"remaining": 3})
    monkeypatch.setattr(CONTROLLER, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(CONTROLLER, "reconcile_contradictory_labels", lambda: [])
    monkeypatch.setattr(CONTROLLER, "list_issues", lambda: [])
    monkeypatch.setattr(CONTROLLER, "list_prs", lambda: [pr])
    monkeypatch.setattr(CONTROLLER, "rotisserie_issue_intake", lambda issues, prs: None)
    monkeypatch.setattr(CONTROLLER, "resolve_review_head_contributors", lambda candidates, prs: candidates)
    monkeypatch.setattr(CONTROLLER, "candidate_is_live_executable", lambda candidate: True)
    monkeypatch.setattr(CONTROLLER, "assign_candidate", lambda candidate, worker: candidate)
    monkeypatch.setattr(CONTROLLER.time, "time", lambda: NOW)
    assert CONTROLLER.assign("46").number == 2264
    monkeypatch.setattr(CONTROLLER.time, "time", lambda: NOW + 10)
    assert CONTROLLER.assign("48") is None
    # A changed comment must replace its cached body, not duplicate an old attempt.
    monkeypatch.setattr(CONTROLLER.time, "time", lambda: NOW + 20)
    assert CONTROLLER.assign("53").number == 2264
    assert page_reads == [15, 2, 1]
    # The controller rounds each cached since boundary down to the minute so
    # near-simultaneous dispatchers share one GitHub request key. The superset
    # is safe because retry-window filtering still uses the original now_epoch.
    assert since_values == [
        _minute_floor(NOW - 86400),
        _minute_floor(NOW - 1),
        _minute_floor(NOW + 9),
    ]
    assert not responses
    logs = capsys.readouterr().err
    assert "source=full pages_read=15" in logs
    assert "source=delta pages_read=2" in logs


def test_new_pass_and_non_cached_callers_read_full_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh pass never inherits a prior pass's rows or delta watermark."""
    calls: list[str] = []

    def github(args: list[str]) -> object:
        calls.append(args[-1])
        return [[_attempt(len(calls), NOW - 10)]]

    monkeypatch.setattr(CONTROLLER, "gh_json", github)
    monkeypatch.setenv("FACTORY_ATTEMPT_HISTORY_CACHE", str(tmp_path / "first-pass.json"))
    CONTROLLER.recent_factory_comments(NOW)
    monkeypatch.setenv("FACTORY_ATTEMPT_HISTORY_CACHE", str(tmp_path / "next-pass.json"))
    assert [row["id"] for row in CONTROLLER.recent_factory_comments(NOW)] == [2]
    monkeypatch.delenv("FACTORY_ATTEMPT_HISTORY_CACHE")
    CONTROLLER.recent_factory_comments(NOW)
    CONTROLLER.recent_factory_comments(NOW)
    assert len(calls) == 4
    assert len(set(calls)) == 1



def test_recent_comment_history_since_uses_minute_floor_for_shared_cache_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nearby dispatches share the same URL without excluding needed comments."""
    requests: list[str] = []
    monkeypatch.delenv("FACTORY_ATTEMPT_HISTORY_CACHE", raising=False)
    def github(args: list[str]) -> object:
        requests.append(args[-1])
        return [[_attempt(1, NOW - 10)]]
    monkeypatch.setattr(CONTROLLER, "gh_json", github)
    CONTROLLER.recent_factory_comments(NOW)
    CONTROLLER.recent_factory_comments(NOW + 10)
    assert requests[0] == requests[1]
    assert "since=" in requests[0]


def test_delta_failure_never_returns_stale_history_or_advances_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed evidence read follows the existing controller failure boundary."""
    cache = tmp_path / "history.json"
    monkeypatch.setenv("FACTORY_ATTEMPT_HISTORY_CACHE", str(cache))
    monkeypatch.setattr(CONTROLLER, "gh_json", lambda args: [[_attempt(1, NOW - 10)]])
    CONTROLLER.recent_factory_comments(NOW)
    before = cache.read_bytes()

    def failure(args: list[str]) -> object:
        raise RuntimeError("second page unavailable")

    monkeypatch.setattr(CONTROLLER, "gh_json", failure)
    with pytest.raises(RuntimeError, match="second page unavailable"):
        CONTROLLER.recent_factory_comments(NOW + 10)
    assert cache.read_bytes() == before


def test_failed_snapshot_commit_preserves_the_previous_complete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed atomic rename cannot leave half-written retry evidence behind."""
    cache = tmp_path / "history.json"
    monkeypatch.setenv("FACTORY_ATTEMPT_HISTORY_CACHE", str(cache))
    monkeypatch.setattr(CONTROLLER, "gh_json", lambda args: [[_attempt(1, NOW - 10)]])
    CONTROLLER.recent_factory_comments(NOW)
    before = cache.read_bytes()

    def cannot_replace(path: Path, target: Path) -> Path:
        raise OSError("snapshot storage unavailable")

    monkeypatch.setattr(Path, "replace", cannot_replace)
    with pytest.raises(RuntimeError, match="could not be persisted"):
        CONTROLLER.recent_factory_comments(NOW + 10)
    assert cache.read_bytes() == before
    assert list(tmp_path.iterdir()) == [cache]


@pytest.mark.parametrize("change", [
    {"version": 2}, {"scope": "other/repo"}, {"reset_seconds": 1},
    {"created_at": NOW - 3601}, {"fetched_at": NOW + 1},
    {"comments": [{"body": "missing identity"}]},
])
def test_unusable_snapshots_refresh_from_authoritative_full_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object],
) -> None:
    """Corrupt, expired or foreign cache data cannot replace trusted GitHub evidence."""
    cache = tmp_path / "history.json"
    cache.write_text(json.dumps({
        "version": 1, "scope": CONTROLLER.REPO, "reset_seconds": 86400,
        "created_at": NOW, "fetched_at": NOW, "full_pages": 15,
        "comments": [_attempt(1, NOW - 10)], **change,
    }))
    monkeypatch.setenv("FACTORY_ATTEMPT_HISTORY_CACHE", str(cache))
    urls: list[str] = []

    def github(args: list[str]) -> object:
        urls.append(args[-1])
        return [[_attempt(2, NOW - 10)]]

    monkeypatch.setattr(CONTROLLER, "gh_json", github)
    assert [row["id"] for row in CONTROLLER.recent_factory_comments(NOW)] == [2]
    since = parse_qs(urlsplit(urls[0]).query)["since"][0]
    assert datetime.fromisoformat(since).timestamp() == _minute_floor(NOW - 86400)


@pytest.mark.parametrize("pages", [[], {}, [{"id": 1}], [[None]]])
def test_incomplete_or_invalid_history_cannot_become_a_cached_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pages: object,
) -> None:
    """Malformed pagination or identities cannot silently unblock retry-limited work."""
    cache = tmp_path / "history.json"
    monkeypatch.setenv("FACTORY_ATTEMPT_HISTORY_CACHE", str(cache))
    monkeypatch.setattr(CONTROLLER, "gh_json", lambda args: pages)
    with pytest.raises(RuntimeError):
        CONTROLLER.recent_factory_comments(NOW)
    assert not cache.exists()


def test_dispatcher_creates_a_new_runner_local_cache_for_its_assignment_step() -> None:
    """Wire the cache into real worker assignment, without leaking it to later passes."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/fixed-model-factory-dispatch.yml").read_text())
    step = next(step for step in workflow["jobs"]["dispatch"]["steps"]
                if step.get("name") == "Resolve and dispatch fixed workers")
    script = step["run"]
    # The module also belongs to the dispatcher's existing control-plane smoke surface.
    assert ".github/scripts/factory_attempt_history.py" in workflow.get("on", workflow.get(True))["push"]["paths"]
    assert 'mktemp -d "$RUNNER_TEMP/factory-attempt-history.XXXXXX"' in script
    assert 'export FACTORY_ATTEMPT_HISTORY_CACHE="$history_cache_dir/comments.json"' in script
    assert script.index("export FACTORY_ATTEMPT_HISTORY_CACHE") < script.index('assign --worker "$worker"')
    assert 'FACTORY_ATTEMPT_HISTORY_CACHE' not in str(step.get("env", {}))


def test_targeted_pr_retry_history_reads_only_its_own_comment_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR wakeup must not paginate all repository comments."""
    monkeypatch.delenv("FACTORY_ATTEMPT_HISTORY_CACHE", raising=False)
    endpoints: list[str] = []
    def github(args: list[str]) -> object:
        endpoints.append(args[-1])
        other = dict(_attempt(2, NOW - 8))
        other["body"] = str(other["body"]).replace("pr-2264:", "pr-2700:")
        return [[_attempt(1, NOW - 10), other]]
    monkeypatch.setattr(CONTROLLER, "gh_json", github)
    records = CONTROLLER.load_no_diff_attempt_records(NOW, target_pr=2264)
    assert len(records) == 1
    assert records[0].number == 2264
    assert records[0].kind == "pr"
    assert len(endpoints) == 1
    assert "repos/JoshCLWren/comic-pile/issues/2264/comments?since=" in endpoints[0]
    assert "repos/JoshCLWren/comic-pile/issues/comments?" not in endpoints[0]


def test_targeted_pr_snapshot_does_not_masquerade_as_full_retry_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACTORY_ATTEMPT_HISTORY_CACHE", str(tmp_path / "history.json"))
    endpoints: list[str] = []
    def github(args: list[str]) -> object:
        endpoints.append(args[-1])
        return [[_attempt(len(endpoints), NOW - 10)]]
    monkeypatch.setattr(CONTROLLER, "gh_json", github)
    CONTROLLER.recent_factory_comments(NOW, target_pr=2264)
    CONTROLLER.recent_factory_comments(NOW)
    assert len(endpoints) == 2
    assert "/issues/2264/comments?" in endpoints[0]
    assert "/issues/comments?" in endpoints[1]


def test_targeted_pr_assignments_use_pr_scoped_retry_history() -> None:
    source = (SCRIPTS / "factory-work-controller.py").read_text()
    assert "load_no_diff_attempt_records(target_pr=target[1])" in source
    assert "if target is not None and target[0] == 'pr'" in source
    assert "else load_no_diff_attempt_records()" in source
