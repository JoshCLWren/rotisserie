"""`auto` picks weigh recent track record inside the eligible live-catalog set."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))

import factory_model_failover as failover  # noqa: E402
import factory_model_track_record as track  # noqa: E402
import factory_opencode_go as go  # noqa: E402
import factory_state_operations_reliability as reliability  # noqa: E402
from factory_runtime_model_select import select  # noqa: E402

SOURCE = "nvidia"
PROVEN = "lab/proven"
WEAK = ("lab/weak-one", "lab/weak-two")
NEW = "lab/brand-new"


def window(completed: int, failed: int, delivered: int | None = None, **kinds: dict) -> dict:
    found = {
        "runs": completed + failed,
        "completed": completed,
        "delivered": completed if delivered is None else delivered,
        "failed": failed,
        "idle": 0,
        "failures": {"work_failure": failed},
    }
    if kinds:
        found["kinds"] = kinds
    return found


def stats_file(tmp_path: Path, rows: dict[str, dict], *, age: timedelta = timedelta(0)) -> Path:
    stamp = (datetime.now(UTC) - age).isoformat()
    path = tmp_path / f"stats-{len(list(tmp_path.iterdir()))}.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": stamp,
                "counts_generated_at": stamp,
                "models": [
                    {"source": SOURCE, "model": model, "windows": {"7d": counts, "24h": counts}}
                    for model, counts in rows.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def catalog(*models: str) -> dict:
    return {"data": [{"id": model} for model in models]}


def picks(models: tuple[str, ...], runs: int = 2000, **kwargs) -> Counter[str]:
    return Counter(
        select(SOURCE, "7", catalog(*models), rotation=rotation, **kwargs)["model"]
        for rotation in range(runs)
    )


@pytest.fixture
def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def install(rows: dict[str, dict], kind: str = "", **options) -> Path:
        path = stats_file(tmp_path, rows, **options)
        monkeypatch.setenv(track.FILE_ENV, str(path))
        monkeypatch.setenv(track.KIND_ENV, kind)
        return path

    monkeypatch.delenv(track.FILE_ENV, raising=False)
    monkeypatch.delenv(track.KIND_ENV, raising=False)
    monkeypatch.delenv(track.PINNED_ENV, raising=False)
    return install


def test_proven_model_wins_most_picks(history) -> None:
    history({PROVEN: window(30, 10), WEAK[0]: window(2, 38), WEAK[1]: window(1, 39)})
    found = picks((PROVEN, *WEAK))
    assert found[PROVEN] / 2000 > 0.6
    assert found.most_common(1)[0][0] == PROVEN


def test_untried_model_gets_the_documented_exploration_share(history) -> None:
    history({PROVEN: window(30, 10)})
    found = picks((PROVEN, NEW))
    assert abs(found[NEW] / 2000 - track.EXPLORE_SHARE) < 0.04


def test_low_sample_model_counts_as_untried(history) -> None:
    history({PROVEN: window(30, 10), NEW: window(0, track.MIN_TRIALS - 1)})
    found = picks((PROVEN, NEW))
    assert abs(found[NEW] / 2000 - track.EXPLORE_SHARE) < 0.04


def test_excluded_model_is_never_picked(history) -> None:
    history({PROVEN: window(40, 0), WEAK[0]: window(1, 39)})
    found = picks((PROVEN, *WEAK, NEW), runs=600, exclusions={SOURCE: [PROVEN]})
    assert PROVEN not in found
    assert set(found) <= {*WEAK, NEW}


def test_credit_pool_exclusion_still_applies(history) -> None:
    history({PROVEN: window(40, 0)})
    priced = {
        "data": [
            {"id": PROVEN, "pricing": {"prompt": "1", "completion": "1"}},
            {"id": WEAK[0], "pricing": {"prompt": "0", "completion": "0"}},
        ]
    }
    for rotation in range(200):
        chosen = select(SOURCE, "7", priced, {"@credit_pool": [SOURCE]}, rotation)
        assert chosen["model"] == WEAK[0]


def test_concurrent_workers_in_one_pass_spread(history) -> None:
    history({PROVEN: window(45, 5), WEAK[0]: window(3, 37), WEAK[1]: window(2, 38)})
    models = (PROVEN, *WEAK, NEW)
    found = Counter(
        select(SOURCE, str(worker), catalog(*models), rotation=99)["model"]
        for worker in range(1, 13)
    )
    assert len(found) >= 2
    assert found[PROVEN] < 12


def test_no_model_takes_more_than_the_share_cap() -> None:
    tried = {
        PROVEN: track.Record(49, 50, "all"),
        WEAK[0]: track.Record(1, 50, "all"),
        WEAK[1]: track.Record(0, 50, "all"),
    }
    shares = track.shares(SOURCE, tried)
    assert shares[PROVEN] == pytest.approx(track.MAX_SHARE)
    assert sum(shares.values()) == pytest.approx(1.0)


def legacy(models: tuple[str, ...], rotation: int) -> str:
    digest = hashlib.sha256(f"{SOURCE}:7:{rotation}".encode()).digest()
    ordered = sorted(models)
    return ordered[int.from_bytes(digest[:8], "big") % len(ordered)]


@pytest.mark.parametrize("setup", ["none", "empty", "stale", "few-trials"])
def test_without_history_the_pick_is_todays_uniform_pick(history, setup: str) -> None:
    if setup == "empty":
        history({})
    elif setup == "stale":
        history({PROVEN: window(40, 0)}, age=timedelta(days=3))
    elif setup == "few-trials":
        history({PROVEN: window(1, 1), WEAK[0]: window(0, 2)})
    models = (PROVEN, *WEAK, NEW)
    for rotation in range(300):
        chosen = select(SOURCE, "7", catalog(*models), rotation=rotation)
        assert chosen["model"] == legacy(models, rotation)


def test_uniform_pick_says_why(history) -> None:
    history({PROVEN: window(1, 1)})
    reason = select(SOURCE, "7", catalog(PROVEN, NEW), rotation=0)["selection_reason"]
    assert reason == "uniform: no eligible nvidia model has 3+ trials in 7d"


def test_same_action_kind_outweighs_overall_rate(history) -> None:
    reviewer, coder = "lab/reviewer", "lab/coder"
    history(
        {
            reviewer: window(10, 30, delivered=0, review=window(10, 2, delivered=0)),
            coder: window(30, 10, review=window(0, 10, delivered=0)),
        },
        kind="review",
    )
    found = picks((reviewer, coder), runs=1000)
    assert found[reviewer] > found[coder]
    reason = select(SOURCE, "7", catalog(reviewer), rotation=1)["selection_reason"]
    assert "10 of 12 review runs" in reason


def test_implement_counts_delivered_prs_not_completions(history) -> None:
    talker, shipper = "lab/talker", "lab/shipper"
    history({talker: window(30, 10, delivered=0), shipper: window(20, 20)}, kind="implement")
    found = picks((talker, shipper), runs=1000)
    assert found[shipper] > found[talker]


def test_idle_and_control_plane_runs_are_not_trials(history) -> None:
    counts = window(4, 6)
    counts["failures"] = {"control_plane_failure": 6}
    counts["idle"] = 20
    history({PROVEN: counts})
    reason = select(SOURCE, "7", catalog(PROVEN), rotation=0)["selection_reason"]
    assert "4 of 4 all runs" in reason


def test_reason_names_score_and_sample_size(history) -> None:
    history({PROVEN: window(30, 10)})
    reason = select(SOURCE, "7", catalog(PROVEN), rotation=0)["selection_reason"]
    assert reason.startswith("track record: 30 of 40 all runs succeeded in 7d; score 0.75")


def test_go_lane_uses_the_same_rule(history, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {"proven-free": window(30, 10), "weak-free": window(1, 39)}
    path = history({})
    data = json.loads(path.read_text())
    data["models"] = [
        {"source": "opencode-free", "model": m, "windows": {"7d": c}} for m, c in rows.items()
    ]
    path.write_text(json.dumps(data))
    monkeypatch.setattr(go, "free_models", lambda payload: ["proven-free", "weak-free"])
    found = Counter(go.choose({}, "3", rotation)["model"] for rotation in range(1000))
    assert found["proven-free"] > 600


def test_failover_explores_instead_of_narrowing_to_last_outcome(history) -> None:
    history({PROVEN: window(30, 10)})
    found = Counter(
        failover.select_next(
            SOURCE,
            "7",
            set(),
            {},
            catalog_json=catalog(PROVEN, NEW),
            rotation=rotation,
            proven={(SOURCE, PROVEN)},
        )["model"]
        for rotation in range(1000)
    )
    assert found[NEW] > 100


def test_failover_without_history_keeps_last_outcome_narrowing(history) -> None:
    found = {
        failover.select_next(
            SOURCE,
            "7",
            set(),
            {},
            catalog_json=catalog(PROVEN, NEW),
            rotation=rotation,
            proven={(SOURCE, PROVEN)},
        )["model"]
        for rotation in range(100)
    }
    assert found == {PROVEN}


@pytest.mark.parametrize(
    ("assignment", "kind"),
    [
        ({"kind": "issue", "stage": "factory:building"}, "implement"),
        ({"kind": "pr", "stage": "factory:review"}, "review"),
        ({"kind": "pr", "stage": "factory:ci"}, "ci-repair"),
        ({"kind": "pr", "stage": "factory:merge-conflict"}, "merge-conflict"),
        ({"kind": "pr", "stage": "factory:changes-requested"}, "review-findings"),
        ({"kind": "none"}, ""),
    ],
)
def test_action_kind_follows_the_planner(assignment: dict, kind: str) -> None:
    assert track.action_kind(assignment) == kind


def test_conflicted_pr_label_makes_a_merge_conflict_action() -> None:
    assignment = {"kind": "pr", "stage": "factory:review"}
    assert track.action_kind(assignment, ["factory:merge-conflict"]) == "merge-conflict"


def test_logged_selection_takes_the_first_marker() -> None:
    log = (
        "2026-11-24T10:00:00Z [factory-model-selection] "
        '{"source": "nvidia", "model": "lab/proven", "kind": "review", "reason": "explore: 0"}\n'
        '2026-11-24T10:05:00Z [factory-model-selection] {"source": "x", "kind": "implement"}\n'
    )
    assert track.logged_selection(log) == {
        "kind": "review", "reason": "nvidia / lab/proven: explore: 0",
    }
    assert track.logged_selection("no marker") == {"kind": "", "reason": ""}


def test_workflows_wire_the_track_record() -> None:
    workflows = ROOT.parent / ".github" / "workflows"
    run = (workflows / "free-model-factory-run.yml").read_text(encoding="utf-8")
    load, rest = run.split("- name: Load model track record", 1)[1].split(
        "- name: Select execution candidate at dispatch time", 1
    )
    assert f"{track.FILE_ENV}=$stats" in load
    assert f"{track.KIND_ENV}=$kind" in load
    assert "factory_model_track_record.py kind" in load
    assert "[factory-model-selection]" in rest.split("- name:", 1)[0]
    entry = (workflows / "free-model-factory-entry.yml").read_text(encoding="utf-8")
    assert "factory_model_track_record.py marker" in entry
    assert ".action_kind = $kind" in entry
    assert "selection: (.selection_reason" in entry


def test_reliability_export_counts_each_action_kind() -> None:
    rows = [
        {
            "window": "7d",
            "source": SOURCE,
            "model": PROVEN,
            "event_type": "worker_completed",
            "delivered": False,
            "outcome_class": "success",
            "action_kind": "review",
            "n": 2,
        },
        {
            "window": "7d",
            "source": SOURCE,
            "model": PROVEN,
            "event_type": "worker_failed",
            "delivered": False,
            "outcome_class": "work_failure",
            "action_kind": "implement",
            "n": 1,
        },
        {
            "window": "7d",
            "source": SOURCE,
            "model": PROVEN,
            "event_type": "worker_completed",
            "delivered": True,
            "outcome_class": "success",
            "action_kind": None,
            "n": 4,
        },
    ]
    report = reliability.summarize_reliability(rows, [], datetime.now(UTC))
    counts = report["models"][0]["windows"]["7d"]
    assert counts["runs"] == 7
    assert counts["kinds"]["review"]["completed"] == 2
    assert counts["kinds"]["implement"]["failures"] == {"work_failure": 1}
    assert "kinds" not in report["models"][0]["windows"]["24h"]


KILO = {
    "lab/free": window(0, 14),
    "lab/small": window(0, 5),
}
KILO_UNTRIED = ("lab/balanced", "lab/efficient", "lab/frontier")


def test_proven_bad_model_gets_no_share_in_a_small_provider(history) -> None:
    history(KILO)
    found = picks((*KILO, *KILO_UNTRIED))
    assert found["lab/free"] == 0
    assert 0 < found["lab/small"] / 2000 < 0.35
    reason = select(SOURCE, "7", catalog(*KILO, *KILO_UNTRIED), rotation=5)["selection_reason"]
    assert reason.startswith("skipped 1 proven-bad; ")


def test_proven_bad_is_absolute_not_relative_to_the_provider() -> None:
    assert track.Record(0, 14, "all").proven_bad
    assert track.Record(0, 16, "review").proven_bad
    assert not track.Record(0, 5, "all").proven_bad
    assert not track.Record(1, 14, "all").proven_bad


def test_a_provider_with_only_proven_bad_models_has_no_pick(history) -> None:
    history({"lab/free": window(0, 14), "lab/other": window(0, 20)})
    with pytest.raises(track.NoUsableModelError):
        select(SOURCE, "7", catalog("lab/free", "lab/other"), rotation=1)


def test_failover_skips_proven_bad_models(history) -> None:
    history({"lab/free": window(0, 14)})
    found = {
        failover.select_next(
            SOURCE, "7", set(), {}, catalog_json=catalog("lab/free", NEW), rotation=rotation
        )["model"]
        for rotation in range(200)
    }
    assert found == {NEW}
    with pytest.raises(failover.ExhaustedCandidatesError):
        failover.select_next(SOURCE, "7", set(), {}, catalog_json=catalog("lab/free"))


def test_an_owner_pinned_provider_keeps_its_proven_bad_models(
    history, monkeypatch: pytest.MonkeyPatch
) -> None:
    history({"lab/free": window(0, 14), "lab/other": window(0, 20)})
    monkeypatch.setenv(track.PINNED_ENV, SOURCE)
    chosen = select(SOURCE, "7", catalog("lab/free", "lab/other"), rotation=1)
    assert chosen["model"] in {"lab/free", "lab/other"}
