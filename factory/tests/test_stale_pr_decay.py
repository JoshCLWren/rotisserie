#!/usr/bin/env python3
"""Regression coverage for the adaptive stale-PR decay guard."""
from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / ".github" / "scripts"
ROOT_SCRIPTS = REPO_ROOT / "scripts"
for entry in (str(ROOT_SCRIPTS), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

SPEC = importlib.util.spec_from_file_location("stale_pr_decay", SCRIPTS / "stale_pr_decay.py")
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

StalePRGuard = module.StalePRGuard
get_staleness_baseline = module.get_staleness_baseline
calculate_staleness_threshold = module.calculate_staleness_threshold
calculate_percentile = module.calculate_percentile
calculate_percentile_threshold = module.calculate_percentile_threshold
calculate_mad_threshold = module.calculate_mad_threshold
is_stale_attempt = module.is_stale_attempt
build_stale_expiration_marker = module.build_stale_expiration_marker
parse_stale_marker = module.parse_stale_marker
pr_lifetime_seconds = module.pr_lifetime_seconds
get_eligible_merged_prs = module.get_eligible_merged_prs
is_factory_implementation_pr = module.is_factory_implementation_pr
linked_issue_from_pr = module.linked_issue_from_pr
get_staleness_observability = module.get_staleness_observability

FACTORY_LABEL = "factory"


def test_stale_pr_decay_cli_imports_shared_eligibility() -> None:
    """Direct CLI execution resolves the same canonical policy as pytest."""
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "stale_pr_decay.py"), "--help"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr




def make_pr(
    number: int,
    *,
    created: str = "2026-08-16T12:00:00Z",
    merged: str | None = None,
    state: str = "MERGED",
    branch: str = "factory/1-100-opencode-free",
    labels: list[str] | None = None,
    body: str = "",
) -> dict[str, Any]:
    """Build a minimal PR payload."""
    pr = {
        "number": number,
        "state": state,
        "createdAt": created,
        "headRefName": branch,
        "labels": [{"name": name} for name in (labels or [FACTORY_LABEL])],
        "body": body,
    }
    if merged is not None:
        pr["mergedAt"] = merged
    return pr


def test_pr_lifetime_merged():
    """Merged PR lifetime is merged_at - created_at."""
    pr = make_pr(1, created="2026-08-16T12:00:00Z", merged="2026-08-16T14:00:00Z")
    lifetime = pr_lifetime_seconds(pr)
    assert lifetime == 7200, f"Expected 7200, got {lifetime}"


def test_pr_lifetime_open():
    """Open PR age uses now - created_at."""
    now = 1_800_000_000
    pr = make_pr(1, created="2026-08-16T12:00:00Z", state="OPEN")
    lifetime = pr_lifetime_seconds(pr, now_epoch=now)
    created_epoch = int(datetime.fromisoformat("2026-08-16T12:00:00+00:00").timestamp())
    expected = now - created_epoch
    assert lifetime == expected, f"Got {lifetime}, expected {expected}"


def test_pr_lifetime_uses_created_not_updated():
    """created_at is used for lifetime, never updated_at."""
    pr = make_pr(1, created="2026-08-16T12:00:00Z", merged="2026-08-17T12:00:00Z")
    lifetime = pr_lifetime_seconds(pr)
    assert lifetime == 86400, f"Expected 86400, got {lifetime}"


def test_calculate_percentile_basic():
    """Percentile calculation works on a simple sorted list."""
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert calculate_percentile(values, 50) == 3.0
    assert calculate_percentile(values, 90) == 4.6


def test_calculate_percentile_single_value():
    """Single-value list returns that value for any percentile."""
    assert calculate_percentile([42.0], 90) == 42.0


def test_calculate_percentile_empty():
    """Empty list returns 0.0."""
    assert calculate_percentile([], 90) == 0.0


def test_calculate_percentile_threshold():
    """2x P90 threshold calculation."""
    values = [3600.0, 7200.0, 10800.0, 18000.0, 25920.0]
    threshold = calculate_percentile_threshold(values)
    p90 = calculate_percentile(values, 90)
    assert threshold == p90 * 2.0


def test_calculate_mad_threshold():
    """Median/MAD threshold is robust to outliers."""
    values = [3600.0, 4000.0, 4400.0, 5000.0, 50000.0]
    threshold = calculate_mad_threshold(values)
    median = statistics_median(values)
    mad = statistics_median([abs(v - median) for v in values])
    assert threshold == median + 2.0 * mad


def statistics_median(values: list[float]) -> float:
    """Helper to match statistics.median behavior."""
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def test_calculate_staleness_threshold_cold_start():
    """Insufficient sample returns cold-start fallback."""
    lifetimes = [3600.0]
    threshold = calculate_staleness_threshold(lifetimes)
    assert threshold == module.COLD_START_FALLBACK_SECONDS


def test_calculate_staleness_threshold_lower_floor():
    """Threshold never goes below the lower floor."""
    lifetimes = [1.0, 2.0, 3.0]
    threshold = calculate_staleness_threshold(lifetimes)
    assert threshold >= module.LOWER_FLOOR_SECONDS


def test_calculate_staleness_threshold_adapts():
    """Threshold adapts as the population changes."""
    small = [3600.0, 7200.0]
    large = [3600.0, 7200.0, 14400.0, 28800.0, 57600.0] * 20
    t1 = calculate_staleness_threshold(small)
    t2 = calculate_staleness_threshold(large)
    assert t2 > t1 or len(large) >= module.MINIMUM_SAMPLE_SIZE


def test_is_stale_attempt():
    """PR is stale when age exceeds threshold."""
    baseline = {"threshold_seconds": 7200, "sample_size": 50}
    assert is_stale_attempt(10000, baseline) is True
    assert is_stale_attempt(3600, baseline) is False
    assert is_stale_attempt(7200, baseline) is False


def test_build_stale_expiration_marker():
    """Marker contains all required fields."""
    baseline = {"sample_size": 50, "formula": "2x_p90", "threshold_seconds": 23200}
    marker = build_stale_expiration_marker(1, 100, 30000, baseline)
    assert "comic-pile-factory-stale-expiration-v1" in marker
    assert "pr-1" in marker
    assert "issue-100" in marker
    assert "age-30000" in marker
    assert "sample-50" in marker
    assert "formula-2x_p90" in marker
    assert "threshold-23200" in marker


def test_parse_stale_marker():
    """Parsing a marker returns the correct fields."""
    body = "<!-- comic-pile-factory-stale-expiration-v1:pr-5:issue-42:age-30000:sample-50:formula-2x_p90:threshold-23200 -->"
    marker = parse_stale_marker(body)
    assert marker is not None
    assert marker["pr"] == 5
    assert marker["issue"] == 42
    assert marker["age"] == 30000
    assert marker["sample"] == 50
    assert marker["formula"] == "2x_p90"
    assert marker["threshold"] == 23200


def test_parse_stale_marker_not_matching():
    """Non-matching body returns None."""
    assert parse_stale_marker("some other text") is None


def test_get_eligible_merged_prs():
    """Only successfully merged Factory PRs are eligible."""
    prs = [
        make_pr(1, merged="2026-08-17T12:00:00Z", branch="factory/1-100-opencode-free"),
        make_pr(2, merged="2026-08-18T12:00:00Z", branch="factory/2-100-opencode-free"),
        make_pr(3, state="OPEN", branch="factory/3-100-opencode-free"),
        make_pr(4, merged="2026-08-19T12:00:00Z", branch="fix/some-fix", labels=["dependencies"]),
    ]
    eligible = get_eligible_merged_prs(prs, limit=10)
    assert len(eligible) == 2
    assert eligible[0]["number"] == 2


def test_is_factory_implementation_pr():
    """Only canonical factory PRs qualify."""
    assert is_factory_implementation_pr(make_pr(1, branch="factory/1-100-opencode-free")) is True
    pr_non_factory = make_pr(2, branch="fix/some-fix", labels=["dependencies"])
    assert is_factory_implementation_pr(pr_non_factory) is False
    pr_infra = make_pr(3, branch="factory/3-100-infra")
    pr_infra["labels"] = [{"name": "factory"}, {"name": "infrastructure"}]
    assert is_factory_implementation_pr(pr_infra) is False


def test_linked_issue_from_pr_branch():
    """Branch shape resolves the issue number."""
    pr = make_pr(1, branch="factory/5-200-opencode-free")
    assert linked_issue_from_pr(pr) == 200


def test_linked_issue_from_pr_body():
    """Closing keyword resolves the issue number."""
    pr = make_pr(1, branch="feature/something", body="Closes #42.")
    assert linked_issue_from_pr(pr) == 42


def test_linked_issue_from_pr_none():
    """No branch or closing keyword returns None."""
    pr = make_pr(1, branch="feature/something", body="Just a reference to #42")
    assert linked_issue_from_pr(pr) is None


def test_get_staleness_baseline_basic():
    """Baseline calculation returns expected structure."""
    now = 1_700_000_000
    prs = [
        make_pr(i, merged=f"2026-08-{16 + i}T12:00:00Z", created=f"2026-08-{16 + i - 1}T12:00:00Z")
        for i in range(1, 20)
    ]
    baseline = get_staleness_baseline(prs, now_epoch=now, limit=100)
    assert baseline["sample_size"] > 0
    assert "threshold_seconds" in baseline
    assert "formula" in baseline
    assert "lower_floor" in baseline


def test_get_staleness_baseline_cold_start():
    """Cold-start fallback when no merged PRs."""
    prs = []
    baseline = get_staleness_baseline(prs)
    assert baseline["sample_size"] == 0
    assert baseline["threshold_seconds"] == module.COLD_START_FALLBACK_SECONDS
    assert baseline["formula"] == "cold-start-fallback"


def test_stale_pr_guard_basic():
    """StalePRGuard evaluates a PR correctly."""
    guard = StalePRGuard()
    pr = make_pr(1, merged="2026-08-17T12:00:00Z", state="MERGED")
    result = guard.evaluate_pr(pr)
    assert "pr" in result
    assert "stale" in result
    assert "age_seconds" in result


def test_stale_pr_guard_not_stale_young_pr():
    """A young PR is not stale."""
    guard = StalePRGuard(rolling_window=10, lower_floor=3600)
    pr = make_pr(1, created="2026-08-20T12:00:00Z", state="OPEN")
    pr["prs"] = [make_pr(i, merged=f"2026-08-{16+i}T12:00:00Z") for i in range(1, 20)]
    result = guard.evaluate_pr(pr, now_epoch=1_700_000_000)
    assert result["stale"] is False


def test_stale_pr_guard_observability():
    """Observability string contains all key metrics."""
    baseline = {
        "sample_size": 50,
        "threshold_seconds": 23200,
        "formula": "2x_p90",
        "p90": 11600,
        "median": 7200,
        "mad": 3600,
        "lower_floor": 3600,
    }
    obs = get_staleness_observability(baseline)
    assert "sample" in obs.lower() or "Sample" in obs
    assert "Threshold" in obs or "threshold" in obs
    assert "Formula" in obs or "formula" in obs


def test_build_stale_expiration_marker_fields():
    """All marker fields are present and correctly formatted."""
    baseline = {
        "sample_size": 100,
        "formula": "2x_p90",
        "threshold_seconds": 82800,
    }
    marker = build_stale_expiration_marker(42, 100, 90000, baseline)
    assert "comic-pile-factory-stale-expiration-v1:pr-42:issue-100" in marker
    assert "age-90000" in marker
    assert "sample-100" in marker
    assert "formula-2x_p90" in marker
    assert "threshold-82800" in marker


def test_stale_expiration_marker_idempotent():
    """Same inputs produce identical markers."""
    baseline = {"sample_size": 50, "formula": "2x_p90", "threshold_seconds": 23200}
    marker1 = build_stale_expiration_marker(1, 100, 30000, baseline)
    marker2 = build_stale_expiration_marker(1, 100, 30000, baseline)
    assert marker1 == marker2


def test_is_factory_implementation_pr_infra_excluded():
    """Infrastructure PRs are excluded from Factory implementation."""
    pr = make_pr(1, branch="factory/1-100-opencode-free", labels=[FACTORY_LABEL, "infrastructure"])
    assert is_factory_implementation_pr(pr) is False
    pr_non_factory = make_pr(2, branch="fix/some-fix", labels=["dependencies"])
    assert is_factory_implementation_pr(pr_non_factory) is False


def test_staleness_threshold_robust_to_outliers():
    """A few old successful PRs do not stretch threshold indefinitely."""
    normal = [3600.0, 7200.0, 14400.0] * 30
    with_outlier = normal + [259200.0, 345600.0]
    t_normal = calculate_staleness_threshold(normal)
    t_with_outlier = calculate_staleness_threshold(with_outlier)
    assert t_with_outlier < t_normal * 5


def test_stale_pr_guard_min_sample_enforcement():
    """Minimum sample size is enforced."""
    guard = StalePRGuard(min_sample=module.MINIMUM_SAMPLE_SIZE)
    prs = [make_pr(i, merged=f"2026-08-{16+i}T12:00:00Z") for i in range(1, 5)]
    baseline = guard.get_baseline(prs)
    assert baseline["sample_size"] < module.MINIMUM_SAMPLE_SIZE
    assert baseline["threshold_seconds"] == module.COLD_START_FALLBACK_SECONDS


def test_pr_lifetime_zero():
    """Same created and merged time gives zero lifetime."""
    pr = make_pr(1, created="2026-08-16T12:00:00Z", merged="2026-08-16T12:00:00Z")
    assert pr_lifetime_seconds(pr) == 0


def test_calculate_staleness_threshold_uses_max_formula():
    """Threshold uses the more conservative of 2x P90 and median/MAD.

    With 5 values below MINIMUM_SAMPLE_SIZE (10), cold-start fallback applies.
    """
    values = [1000.0, 2000.0, 3000.0, 4000.0, 100000.0]
    threshold = calculate_staleness_threshold(values)
    # With 5 values, cold-start fallback (86400) is used since len < 10
    assert threshold == module.COLD_START_FALLBACK_SECONDS

    # With enough samples, the formula should apply
    many_values = values * 30
    threshold_many = calculate_staleness_threshold(many_values)
    p90_threshold = calculate_percentile_threshold(many_values)
    mad_threshold = calculate_mad_threshold(many_values)
    assert threshold_many >= p90_threshold
    assert threshold_many >= mad_threshold


def test_stale_pr_guard_empty_population():
    """Empty population produces cold-start baseline."""
    guard = StalePRGuard()
    baseline = guard.get_baseline([])
    assert baseline["sample_size"] == 0
    assert baseline["threshold_seconds"] == module.COLD_START_FALLBACK_SECONDS


def test_get_eligible_merged_prs_limit():
    """Limit parameter caps the result size."""
    prs = [make_pr(i, merged=f"2026-08-{16+i}T12:00:00Z") for i in range(1, 200)]
    eligible = get_eligible_merged_prs(prs, limit=10)
    assert len(eligible) == 10


def test_get_eligible_merged_prs_sorted_desc():
    """Eligible PRs are sorted by merge time descending."""
    prs = [
        make_pr(1, merged="2026-08-18T12:00:00Z"),
        make_pr(2, merged="2026-08-20T12:00:00Z"),
        make_pr(3, merged="2026-08-19T12:00:00Z"),
    ]
    eligible = get_eligible_merged_prs(prs)
    assert eligible[0]["number"] == 2
    assert eligible[1]["number"] == 3
    assert eligible[2]["number"] == 1


def test_get_staleness_baseline_with_non_factory_prs():
    """Non-Factory PRs are filtered out from baseline."""
    prs = [
        make_pr(1, merged="2026-08-17T12:00:00Z"),
        {"number": 2, "state": "MERGED", "createdAt": "2026-08-16T12:00:00Z",
         "mergedAt": "2026-08-17T12:00:00Z", "headRefName": "dependabot/update",
         "labels": [{"name": "dependencies"}]},
    ]
    baseline = get_staleness_baseline(prs)
    assert baseline["sample_size"] == 1


def test_parse_stale_marker_partial():
    """Partial marker text still parses correctly."""
    body = "Some text <!-- comic-pile-factory-stale-expiration-v1:pr-7:issue-8:age-100:sample-50:formula-2x_p90:threshold-23200 --> more text"
    marker = parse_stale_marker(body)
    assert marker is not None
    assert marker["pr"] == 7


def test_calculate_percentile_interpolation():
    """Linear interpolation between values is correct."""
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    result = calculate_percentile(values, 25)
    assert result == 20.0, f"Expected 20.0, got {result}"
    result = calculate_percentile(values, 75)
    assert result == 40.0, f"Expected 40.0, got {result}"


def test_stale_pr_guard_with_custom_config():
    """Custom configuration is respected."""
    guard = StalePRGuard(rolling_window=5, multiplier=1.5, lower_floor=1800)
    assert guard.rolling_window == 5
    assert guard.multiplier == 1.5
    assert guard.lower_floor == 1800


def test_get_staleness_baseline_sample_size_cap():
    """Baseline does not exceed the rolling window."""
    now = 1_700_000_000
    prs = [make_pr(i, merged=f"2026-08-{16+i}T12:00:00Z") for i in range(1, 200)]
    baseline = get_staleness_baseline(prs, now_epoch=now, limit=10)
    assert baseline["sample_size"] <= 10


def test_record_stale_expiration_refuses_legacy_age_based_close(monkeypatch) -> None:
    """Even an explicit legacy helper call cannot destroy work due to age."""
    def forbidden(*_args, **_kwargs) -> None:
        raise AssertionError("no GitHub write is allowed by stale-age cleanup")

    for action in ("reset_issue_to_unowned", "close_pr_implementation", "run_gh"):
        monkeypatch.setattr(module, action, forbidden)
    try:
        module.record_stale_expiration(
            42, 100, 90_000,
            {"sample_size": 50, "formula": "2x_p90", "threshold_seconds": 23_200},
        )
    except RuntimeError as exc:
        assert "age-based PR expiration is disabled" in str(exc)
    else:
        raise AssertionError("the legacy expiration helper must fail closed")


def test_reset_issue_to_unowned_makes_open_issue_executable(monkeypatch) -> None:
    """Stale cleanup returns an open linked issue to building intake."""
    monkeypatch.setattr(
        module,
        "gh_json",
        lambda args, **kwargs: {"state": "open", "labels": []},
    )
    writes: list[tuple[int, str, str]] = []
    monkeypatch.setattr(
        module,
        "replace_factory_labels",
        lambda number, owner, stage=None: writes.append((number, owner, stage)),
    )

    module.reset_issue_to_unowned(100)

    assert writes == [(100, "factory:unowned", "factory:building")]


def test_reset_issue_to_unowned_respects_terminal_closed_issue(monkeypatch) -> None:
    """A separately closed product issue is not resurrected by stale PR cleanup."""
    monkeypatch.setattr(
        module,
        "gh_json",
        lambda args, **kwargs: {"state": "closed", "state_reason": "not_planned"},
    )
    writes: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        module,
        "replace_factory_labels",
        lambda number, owner, stage=None: writes.append((number, owner, stage)),
    )

    module.reset_issue_to_unowned(100)

    assert writes == []


def test_get_pr_comments_flattens_slurped_pages(monkeypatch) -> None:
    """Paginated ``--slurp`` pages are flattened into one comment list."""
    pages = [[{"body": "first"}], [{"body": "second"}, {"body": "third"}]]
    monkeypatch.setattr(module, "gh_json", lambda _args: pages)

    assert module.get_pr_comments(42) == [
        {"body": "first"},
        {"body": "second"},
        {"body": "third"},
    ]


def test_process_stale_prs_skips_reopened_pr_with_trusted_marker(monkeypatch) -> None:
    """A reopened attempt carrying its original marker is not expired again."""
    now = 1_800_000_000
    created = datetime.fromtimestamp(now - 200_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    pr = make_pr(9, created=created, state="OPEN")
    baseline = {
        "sample_size": 50,
        "threshold_seconds": 90_000,
        "formula": "2x_p90_vs_mad_max",
    }
    marker = build_stale_expiration_marker(9, 100, 200_000, baseline)
    closed: list[int] = []
    monkeypatch.setattr(module, "get_staleness_baseline", lambda *_args, **_kwargs: baseline)
    monkeypatch.setattr(
        module,
        "get_pr_comments",
        lambda _number: [{"body": marker, "author_association": "OWNER"}],
    )
    monkeypatch.setattr(
        module,
        "record_stale_expiration",
        lambda pr_number, *_args: closed.append(pr_number) or "marker",
    )

    results = module.process_stale_prs(prs=[pr], now_epoch=now)

    assert results == [
        {"pr": 9, "status": "already-expired", "age_seconds": 200_000}
    ]
    assert closed == []


def test_stale_pr_cutover_deferral_preserves_open_pr(monkeypatch) -> None:
    """A deferred issue's existing PR must survive long waits until #4335 closes."""
    now = 1_800_000_000
    created = datetime.fromtimestamp(now - 700_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    pr = make_pr(
        4704, created=created, state="OPEN",
        branch="factory/45-3507-opencode-free",
    )
    baseline = {"sample_size": 50, "threshold_seconds": 90_000}
    monkeypatch.setattr(module, "get_staleness_baseline", lambda *_a, **_kw: baseline)
    monkeypatch.setattr(module, "get_pr_comments", lambda _n: [])
    monkeypatch.setattr(
        module, "gh_json",
        lambda args: (
            {"state": "open"} if args[1].endswith("/4335")
            else {
                "state": "open", "labels": [],
                "body": "Depends on #4335",
            }
        ),
    )
    closed: list[int] = []
    monkeypatch.setattr(
        module, "record_stale_expiration",
        lambda pr_number, *_args: closed.append(pr_number) or "marker",
    )
    result = module.process_stale_prs(prs=[pr], now_epoch=now)
    assert result[0]["status"] == "cutover-deferred"
    assert closed == []


def test_stale_pr_cutover_issue_is_reported_without_closing(monkeypatch) -> None:
    """Even active Rust attempts keep their exact-head code when old."""
    now = 1_800_000_000
    created = datetime.fromtimestamp(now - 700_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    pr = make_pr(
        4703, created=created, state="OPEN",
        branch="factory/46-4589-kilo-auto",
    )
    baseline = {"sample_size": 50, "threshold_seconds": 90_000}
    monkeypatch.setattr(module, "get_staleness_baseline", lambda *_a, **_kw: baseline)
    monkeypatch.setattr(module, "get_pr_comments", lambda _n: [])
    monkeypatch.setattr(
        module, "gh_json",
        lambda args: (
            {"state": "open"} if args[1].endswith("/4335")
            else {"state": "open", "labels": [{"name": "rust"}], "body": ""}
        ),
    )
    closed: list[int] = []
    monkeypatch.setattr(
        module, "record_stale_expiration",
        lambda pr_number, *_args: closed.append(pr_number) or "marker",
    )
    result = module.process_stale_prs(prs=[pr], now_epoch=now)
    assert result[0]["status"] == "stale-preserved"
    assert result[0]["issue"] == 4589
    assert closed == []


def test_stale_pr_cutover_state_lookup_failure_cannot_close_pr(monkeypatch) -> None:
    """Transient GitHub failures must never delete potentially deferred work."""
    now = 1_800_000_000
    created = datetime.fromtimestamp(now - 700_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    pr = make_pr(4704, created=created, state="OPEN")
    monkeypatch.setattr(
        module, "get_staleness_baseline",
        lambda *_a, **_kw: {"sample_size": 50, "threshold_seconds": 90_000},
    )
    monkeypatch.setattr(module, "get_pr_comments", lambda _n: [])
    def no_api(_args: list[str]) -> None:
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(module, "gh_json", no_api)
    closed: list[int] = []
    monkeypatch.setattr(
        module, "record_stale_expiration",
        lambda pr_number, *_args: closed.append(pr_number) or "marker",
    )
    result = module.process_stale_prs(prs=[pr], now_epoch=now)
    assert result[0]["status"] == "cutover-check-unavailable"
    assert closed == []


def test_process_stale_prs_does_not_close_manual_only_pr(monkeypatch) -> None:
    """A factory PR with the shared manual-only marker must stay open.

    Stale expiration ages from created_at, so a reopen of #3971/#3970 still
    looks ~621k seconds old versus the 86400s cold-start threshold. Skip the
    close instead of inventing a second hold marker.
    """
    now = 1_800_000_000
    created = datetime.fromtimestamp(now - 700_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    source = (SCRIPTS / "stale_pr_decay.py").read_text(encoding="utf-8")
    assert "from factory_work_policy import is_manual_only" in source
    hold_body = "<!-- factory-execution:manual-only -->\nHold this factory PR.\n"
    assert module.is_manual_only(hold_body)
    hold = make_pr(
        7,
        created=created,
        state="OPEN",
        body=hold_body,
    )
    ordinary = make_pr(
        8,
        created=created,
        state="OPEN",
        body="Ordinary autonomous factory work.\n",
    )
    closed: list[int] = []
    comment_fetches: list[int] = []
    monkeypatch.setattr(
        module,
        "get_pr_comments",
        lambda number: comment_fetches.append(number) or [],
    )
    monkeypatch.setattr(
        module,
        "record_stale_expiration",
        lambda pr_number, issue_number, age_seconds, baseline: (
            closed.append(pr_number) or "marker"
        ),
    )

    monkeypatch.setattr(module, "stale_pr_is_cutover_deferred", lambda _pr: False)
    results = module.process_stale_prs(prs=[hold, ordinary], now_epoch=now)
    by_pr = {int(result["pr"]): result for result in results}

    assert by_pr[7]["status"] == "manual-only"
    assert 7 not in closed
    assert 7 not in comment_fetches
    assert closed == []
    assert by_pr[8]["status"] == "stale-preserved"
    assert by_pr[8]["dry_run"] is False


def test_age_only_preserves_review_claim_commit_and_issue_even_in_normal_mode(monkeypatch) -> None:
    """Age-based cleanup must not erase a repairable Rust PR or its worker lease."""
    now = 1_800_000_000
    created = datetime.fromtimestamp(now - 700_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    labels = ["factory", "factory:changes-requested", "factory:80", "rust"]
    pr = make_pr(
        4727,
        created=created,
        state="OPEN",
        branch="factory/54-4719-opencode-free",
        labels=labels,
        body="Part of #4719. Retain this scoped migration PR.",
    )
    baseline = {"sample_size": 50, "threshold_seconds": 90_000}
    monkeypatch.setattr(module, "get_staleness_baseline", lambda *_a, **_kw: baseline)
    monkeypatch.setattr(module, "get_pr_comments", lambda _n: [])
    monkeypatch.setattr(module, "stale_pr_is_cutover_deferred", lambda _pr: False)

    def forbid_destructive_call(*_args, **_kwargs) -> None:
        raise AssertionError("age-only stale evaluation must never write or close")

    for destructive in (
        "record_stale_expiration",
        "reset_issue_to_unowned",
        "close_pr_implementation",
        "replace_factory_labels",
        "run_gh",
    ):
        monkeypatch.setattr(module, destructive, forbid_destructive_call)

    for dry_run in (False, True):
        result = module.process_stale_prs(prs=[pr], now_epoch=now, dry_run=dry_run)
        assert result == [{
            "pr": 4727,
            "status": "stale-preserved",
            "age_seconds": 700_000,
            "issue": 4719,
            "baseline": baseline,
            "dry_run": dry_run,
            "jev": {"status": "unavailable", "reason": "missing dedicated Jev key"},
        }]
        assert [label["name"] for label in pr["labels"]] == labels


def test_stale_report_preserves_branch_without_linked_issue(monkeypatch) -> None:
    """A local factory PR without a closing keyword keeps its actual patch."""
    now = 1_800_000_000
    created = datetime.fromtimestamp(now - 700_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    pr = make_pr(
        4778, created=created, state="OPEN",
        branch="local/issue-card-roll-featured-parity-20261107",
        body="Part of #4445",
    )
    monkeypatch.setattr(
        module, "get_staleness_baseline",
        lambda *_a, **_kw: {"sample_size": 20, "threshold_seconds": 90_000},
    )
    monkeypatch.setattr(module, "get_pr_comments", lambda _n: [])
    monkeypatch.setattr(module, "stale_pr_is_cutover_deferred", lambda _pr: False)
    monkeypatch.setattr(
        module,
        "record_stale_expiration",
        lambda *_a: (_ for _ in ()).throw(AssertionError("closed valuable code")),
    )
    result = module.process_stale_prs(prs=[pr], now_epoch=now)
    assert result[0]["status"] == "stale-preserved"
    assert result[0]["issue"] is None


def test_stale_jev_preserves_incomplete_work_even_when_model_suggests_closure() -> None:
    """Jev is consulted but cannot directly expire valuable commits."""
    prs = [
        make_pr(
            4727, state="OPEN", body="Part of #4719. The migration is unfinished.",
            labels=["factory", "factory:changes-requested", "rust"],
        ),
        make_pr(4758, state="OPEN", body="Only partial SQL proof exists."),
    ]
    calls: list[dict[str, Any]] = []

    def jev(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        assert api_key == "test-jev-key"
        calls.append(payload)
        return {
            "model": module.JEV_MODEL,
            "answers": {
                "closure_4727": {
                    "type": "choice", "choice": "preserve",
                    "confidence": 0.99,
                    "probabilities": {"preserve": 0.99, "closure_review": 0.01},
                },
                "closure_4758": {
                    "type": "choice", "choice": "closure_review",
                    "confidence": 0.95,
                    "probabilities": {"preserve": 0.04, "closure_review": 0.96},
                },
            },
        }

    decisions = module.consult_jev_on_aged_prs(prs, "test-jev-key", client=jev)
    assert len(calls) == 1
    assert calls[0]["model"] == module.JEV_MODEL
    assert decisions[4727]["status"] == "preserve"
    # High confidence authorizes a separate investigation, not automatic close.
    assert decisions[4758]["status"] == "closure-review"


def test_stale_jev_unavailable_abstains_without_destructive_fallback() -> None:
    """Provider interruptions and malformed answers always retain the old PR."""
    prs = [make_pr(4727, state="OPEN")]
    no_key = module.consult_jev_on_aged_prs(prs, "")
    assert no_key[4727]["status"] == "unavailable"
    assert no_key[4727]["reason"] == "missing dedicated Jev key"

    def unavailable(_payload: dict[str, Any], _key: str) -> dict[str, Any]:
        raise OSError("provider unreachable")

    result = module.consult_jev_on_aged_prs(prs, "test-jev-key", client=unavailable)
    assert result[4727]["status"] == "unavailable"

    invalid = module.consult_jev_on_aged_prs(
        prs, "test-jev-key",
        client=lambda _payload, _key: {
            "model": module.JEV_MODEL,
            "answers": {
                "closure_4727": {
                    "type": "choice", "choice": "closure_review",
                    "confidence": 0.8,
                    "probabilities": {"preserve": -1, "closure_review": 2},
                },
            },
        },
    )
    assert invalid[4727]["status"] == "unavailable"


def test_stale_jev_weak_closure_decision_is_preservation() -> None:
    """Low-confidence choice does not promote a PR into closure investigation."""
    prs = [make_pr(4727, state="OPEN")]
    result = module.consult_jev_on_aged_prs(
        prs, "test-jev-key",
        client=lambda _payload, _key: {
            "model": module.JEV_MODEL,
            "answers": {
                "closure_4727": {
                    "type": "choice", "choice": "closure_review",
                    "confidence": 0.55,
                    "probabilities": {"preserve": 0.4, "closure_review": 0.6},
                },
            },
        },
    )
    assert result[4727]["status"] == "preserve"


def test_stale_guard_standalone_imports_shared_policy(tmp_path: Path) -> None:
    """A fresh process loads shared eligibility without another test priming imports."""
    import os
    import subprocess

    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "stale_pr_decay.py"), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
