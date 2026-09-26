"""Unit tests for OpenCode CLI factory catalog discovery and retirement."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "opencode-catalog"


def _load(name: str, filename: str) -> ModuleType:
    """Load a ``.github/scripts`` module without packaging that tree."""
    sys.path.insert(0, str(SCRIPTS))
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROSTER = _load("factory_roster_discovery_test", "factory_roster.py")
CATALOG = _load("factory_model_catalog_test", "factory_model_catalog.py")
RETIRE = _load("factory_model_retirement_test", "factory_model_retirement.py")


def _row(
    worker: str,
    source: str,
    model: str,
    minute: str = "5",
) -> dict[str, str]:
    """Build one roster row for planner tests."""
    return {
        "worker": worker,
        "source": source,
        "model": model,
        "minute": minute,
        "scheduler": "dispatcher",
        "display_name": model,
    }


def _assert_validator_minutes(rows: list[dict[str, str]]) -> None:
    """Apply the discovery validator's minute loop that crashed on ``int('')``.

    Mirrors ``validate-free-model-factories.py`` so grow-path apply fixtures
    prove the PR step would stay green after writing the TSV.
    """
    counts: Counter[int] = Counter()
    for row in rows:
        minute = int(row["minute"])
        assert row["scheduler"] == "dispatcher"
        assert minute in ROSTER.SCHEDULE_MINUTES
        counts[minute] += 1
    assert set(counts) == set(ROSTER.SCHEDULE_MINUTES)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert sum(counts.values()) == len(rows)
    assert ROSTER.schedule_is_balanced(rows)


def test_committed_lock_matches_live_roster() -> None:
    """The generated expected-worker lock must stay in sync with the TSV."""
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    lock = ROSTER.load_roster_lock(ROOT / ".github" / "factory-expected-workers.json")
    assert set(lock["expected_workers"]) == ROSTER.roster_worker_ids(rows)
    assert {40, 43, 44}.issubset(set(lock["retired_workers"]))


def test_catalog_miss_retires_opencode_pin() -> None:
    """A pin missing from ``opencode models opencode`` is retired."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "catalog-miss.json")
    rows = [
        _row("41", "opencode-free", "mimo-v2.5-free"),
        _row("39", "opencode-free", "big-pickle"),
        _row("46", "kilo-auto", "kilo-auto/free"),
    ]

    plan = RETIRE.plan_retirement(rows, catalogs)

    retired = {item.model: item for item in plan.retirements}
    assert "mimo-v2.5-free" in retired
    assert "absent from opencode models opencode" in retired["mimo-v2.5-free"].reason
    kept = {item.model for item in plan.kept}
    assert "big-pickle" in kept
    assert "kilo-auto/free" in kept


def test_cost_zero_present_keeps_opencode_pin() -> None:
    """A free cost-0 model still listed by OpenCode stays on the roster."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = [
        _row("39", "opencode-free", "big-pickle"),
        _row("41", "opencode-free", "mimo-v2.6-flash-free"),
        _row("47", "opencode-free", "muse-spark-1.2-contributor-free"),
    ]

    plan = RETIRE.plan_retirement(rows, catalogs)

    assert plan.retirements == ()
    assert {item.model for item in plan.kept} == {
        "big-pickle",
        "mimo-v2.6-flash-free",
        "muse-spark-1.2-contributor-free",
    }


def test_paid_model_is_not_proposed_for_opencode_free() -> None:
    """Paid Zen models never enter the unused-free discovery report."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = [_row("39", "opencode-free", "big-pickle")]

    plan = RETIRE.plan_retirement(rows, catalogs)
    unused = {item.model for item in plan.unused_free}

    assert "deepseek-v4-flash" not in unused
    assert "deepseek-v4-flash" in plan.paid_rejected
    assert "ling-3.0-flash-fin-free" in unused
    assert "muse-spark-1.3-contributor-free" in unused
    added = {item.model for item in plan.additions}
    assert "deepseek-v4-flash" not in added
    assert "ling-3.0-flash-fin-free" in added
    assert "muse-spark-1.3-contributor-free" in added
    assert RETIRE.classify_opencode_free_eligibility(
        "deepseek-v4-flash",
        catalogs["opencode"].get("deepseek-v4-flash"),
    ) is False


def test_nvidia_kept_when_opencode_lists_even_if_integrate_api_omits() -> None:
    """NVIDIA pins follow ``opencode models nvidia``, not integrate.api dumps."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "catalog-miss.json")
    payload = json.loads((FIXTURES / "catalog-miss.json").read_text(encoding="utf-8"))
    assert "stepfun-ai/step-3.7-flash" in payload["integrate_api_missing"]
    rows = [_row("9", "nvidia", "stepfun-ai/step-3.7-flash")]

    plan = RETIRE.plan_retirement(rows, catalogs)

    assert plan.retirements == ()
    assert plan.kept[0].model == "stepfun-ai/step-3.7-flash"
    assert "opencode models nvidia" in plan.kept[0].reason


def _410_comment(model: str, source: str = "nvidia") -> dict[str, str]:
    """Build one #1093 NVIDIA-probe-shaped 410 retirement comment."""
    return {
        "body": (
            f"{RETIRE.RETIREMENT_MARKER}\n"
            f"Source: {source}\n"
            f"Model: {model}\n"
            "Reason: provider returned HTTP 410 Gone\n"
            "Updated: 2026-09-14T00:00:00Z\n"
        )
    }


def _nvidia_410_zombie_rows() -> list[dict[str, str]]:
    """Return a balanced roster with two catalog-present NVIDIA 410 zombies.

    Twelve keepers occupy every dispatcher minute. Workers 9 and 14 sit on
    :05 and :20 with models still listed by ``opencode models nvidia``.
    Only 410 comments should retire them.
    """
    keepers = [str(worker) for worker in range(101, 113)]
    rows = [
        _row(worker, "nvidia", "poolside/laguna-xs-2.1", minute=str(minute))
        for worker, minute in zip(keepers, ROSTER.SCHEDULE_MINUTES, strict=True)
    ]
    rows.append(_row("9", "nvidia", "stepfun-ai/step-3.7-flash", minute="5"))
    rows.append(_row("14", "nvidia", "minimaxai/minimax-m3", minute="20"))
    assert ROSTER.schedule_is_balanced(rows)
    return rows


def test_retirement_marker_matches_probe_and_health_selector() -> None:
    """Planner, probe, and health selector share the same 410 marker string."""
    health = (SCRIPTS / "factory_candidate_health.py").read_text(encoding="utf-8")
    runner = (
        ROOT / ".github" / "workflows" / "free-model-factory-run.yml"
    ).read_text(encoding="utf-8")
    assert f'RETIREMENT_MARKER = "{RETIRE.RETIREMENT_MARKER}"' in health
    assert f"retirement_marker='{RETIRE.RETIREMENT_MARKER}'" in runner


def test_nvidia_410_comments_extract_bare_model_ids() -> None:
    """Parser follows the NVIDIA probe: marker line, Source nvidia, Model id."""
    payload = json.loads(
        (FIXTURES / "nvidia-410-comments.json").read_text(encoding="utf-8")
    )
    models = RETIRE.nvidia_models_retired_by_410_comments(payload)
    assert models == {
        "stepfun-ai/step-3.7-flash",
        "nvidia/nemotron-3-nano-30b-a3b",
        "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "minimaxai/minimax-m3",
    }
    slurped = RETIRE.nvidia_models_retired_by_410_comments([payload[:2], payload[2:]])
    assert slurped == models
    ignored = RETIRE.nvidia_models_retired_by_410_comments(
        [_410_comment("stepfun-ai/step-3.7-flash", source="openrouter-free")]
    )
    assert ignored == frozenset()


def test_plan_retires_nvidia_410_markers_even_when_opencode_lists_them() -> None:
    """A sticky #1093 410 comment retires a pin still in the nvidia catalog."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = _nvidia_410_zombie_rows()
    payload = json.loads(
        (FIXTURES / "nvidia-410-comments.json").read_text(encoding="utf-8")
    )
    retired_410 = RETIRE.nvidia_models_retired_by_410_comments(payload)

    plan = RETIRE.plan_retirement(
        rows,
        catalogs,
        retired_410_models=retired_410,
    )

    retired = {item.worker: item for item in plan.retirements}
    assert set(retired) == {"9", "14"}
    assert retired["9"].model == "stepfun-ai/step-3.7-flash"
    assert retired["14"].model == "minimaxai/minimax-m3"
    assert all(item.reason == RETIRE.NVIDIA_410_REASON for item in plan.retirements)
    kept_models = {item.model for item in plan.kept}
    assert "poolside/laguna-xs-2.1" in kept_models
    assert "stepfun-ai/step-3.7-flash" not in kept_models
    assert "minimaxai/minimax-m3" not in kept_models


def test_apply_rebalances_after_nvidia_410_zombie_pins(tmp_path: Path) -> None:
    """Apply drops 410-zombie pins, rebalances minutes, and syncs the lock.

    This is the catalog-absent retirement operator path with 410 comments as
    the turned-off store. It is not a hand-edit of workers 9 and 14.
    """
    rows = _nvidia_410_zombie_rows()
    roster = tmp_path / "free-model-factories.tsv"
    lock_path = tmp_path / "factory-expected-workers.json"
    comments = tmp_path / "issue-1093-comments.json"
    ROSTER.write_roster_rows(
        roster,
        rows,
        comments=("# worker\tsource\tmodel\tminute\tscheduler\tdisplay_name",),
    )
    ROSTER.sync_roster_lock(rows, lock_path=lock_path)
    comments.write_text(
        (FIXTURES / "nvidia-410-comments.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    status = RETIRE.run(
        [
            "apply",
            "--roster",
            str(roster),
            "--lock",
            str(lock_path),
            "--catalog-json",
            str(FIXTURES / "keep-present.json"),
            "--retirement-comments",
            str(comments),
            "--no-add-unused-free",
        ]
    )
    remaining = ROSTER.load_roster_rows(roster)
    lock = ROSTER.load_roster_lock(lock_path)
    remaining_ids = {row["worker"] for row in remaining}

    assert status == 0
    assert remaining_ids.isdisjoint({"9", "14"})
    assert "101" in remaining_ids
    assert ROSTER.schedule_is_balanced(remaining)
    counts = ROSTER.schedule_counts(remaining)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert set(lock["expected_workers"]) == ROSTER.roster_worker_ids(remaining)
    assert {9, 14}.issubset(set(lock["retired_workers"]))
    assert {
        "stepfun-ai/step-3.7-flash",
        "minimaxai/minimax-m3",
    }.issubset(set(lock["retired_models"]))


def test_cli_plan_fails_closed_on_invalid_retirement_comments(tmp_path: Path) -> None:
    """A malformed #1093 comments dump must not silently skip 410 evidence."""
    roster = tmp_path / "roster.tsv"
    comments = tmp_path / "comments.json"
    roster.write_text(
        "9\tnvidia\tstepfun-ai/step-3.7-flash\t5\tdispatcher\tStep\n",
        encoding="utf-8",
    )
    comments.write_text("{not-json", encoding="utf-8")

    status = RETIRE.run(
        [
            "plan",
            "--roster",
            str(roster),
            "--catalog-json",
            str(FIXTURES / "keep-present.json"),
            "--retirement-comments",
            str(comments),
        ]
    )

    assert status == 2


def test_kilo_auto_and_big_pickle_stay_unless_catalog_absent() -> None:
    """kilo-auto is never catalog-retired; big-pickle stays while listed."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "catalog-miss.json")
    rows = [
        _row("46", "kilo-auto", "kilo-auto/free"),
        _row("39", "opencode-free", "big-pickle"),
    ]

    plan = RETIRE.plan_retirement(rows, catalogs)

    assert plan.retirements == ()
    by_model = {item.model: item for item in plan.kept}
    assert "not enumerated" in by_model["kilo-auto/free"].reason
    assert by_model["big-pickle"].action == "keep"


# Temporary IDs only. Never read the live TSV for clustered-retirement
# coverage: production pins such as 10/16/17/18/19/66/67/70 disappear after
# a real apply and must not be the only way this suite proves rebalance.
SYNTHETIC_RETIRE_WORKERS = tuple(str(worker) for worker in range(801, 809))
SYNTHETIC_KEEP_WORKERS = tuple(str(worker) for worker in range(901, 929))


def _plan_for_workers(
    rows: list[dict[str, str]],
    workers: set[str],
) -> RETIRE.RetirementPlan:
    """Build a retirement plan that drops the named workers only."""
    return RETIRE.RetirementPlan(
        retirements=tuple(
            RETIRE.PinDecision(
                worker=row["worker"],
                source=row["source"],
                model=row["model"],
                action="retire",
                reason="absent from opencode models nvidia",
            )
            for row in rows
            if row["worker"] in workers
        ),
        kept=(),
        unused_free=(),
        paid_rejected=(),
        catalog_sources={},
    )


def _clustered_retirement_rows() -> tuple[list[dict[str, str]], frozenset[str], frozenset[str]]:
    """Build a 36-slot roster where eight clustered pins unbalance minutes.

    Every dispatcher minute starts with three workers. Four minutes hold two
    synthetic retirees each; after those eight pins drop, those buckets have
    one worker while the other eight stay at three. Worker ids live in the
    800/900 range so the proof never depends on the live factory TSV.

    Returns:
        Roster rows, retire worker ids, and retire model ids.
    """
    clustered_minutes = ROSTER.SCHEDULE_MINUTES[:4]
    filled_minutes = ROSTER.SCHEDULE_MINUTES[4:]
    keep_iter = iter(SYNTHETIC_KEEP_WORKERS)
    retire_iter = iter(SYNTHETIC_RETIRE_WORKERS)
    rows: list[dict[str, str]] = []
    retire_workers: list[str] = []
    retire_models: list[str] = []

    for minute in clustered_minutes:
        keeper = next(keep_iter)
        rows.append(_row(keeper, "nvidia", f"synthetic/keep-{keeper}", minute=str(minute)))
        for _index in range(2):
            worker = next(retire_iter)
            retire_workers.append(worker)
            if int(worker) <= 805:
                model = f"synthetic/retire-{worker}"
                source = "nvidia"
            else:
                model = f"synthetic/retire-{worker}:free"
                source = "openrouter-free"
            retire_models.append(model)
            rows.append(_row(worker, source, model, minute=str(minute)))

    for minute in filled_minutes:
        for _index in range(3):
            keeper = next(keep_iter)
            rows.append(_row(keeper, "nvidia", f"synthetic/keep-{keeper}", minute=str(minute)))

    assert next(keep_iter, None) is None
    assert next(retire_iter, None) is None
    assert ROSTER.schedule_is_balanced(rows)
    live_ids = {
        str(worker)
        for worker in ROSTER.roster_worker_ids(
            ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
        )
    }
    assert set(SYNTHETIC_RETIRE_WORKERS).isdisjoint(live_ids)
    assert set(SYNTHETIC_KEEP_WORKERS).isdisjoint(live_ids)
    return rows, frozenset(retire_workers), frozenset(retire_models)


def _write_clustered_retirement_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, list[dict[str, str]], frozenset[str], frozenset[str]]:
    """Write a temporary roster, lock, and catalog for clustered retirement.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Roster path, lock path, catalog path, rows, retire workers, and
        retire models.
    """
    rows, retire_workers, retire_models = _clustered_retirement_rows()
    roster = tmp_path / "free-model-factories.tsv"
    lock_path = tmp_path / "factory-expected-workers.json"
    catalog = tmp_path / "catalog-clustered-miss.json"
    ROSTER.write_roster_rows(
        roster,
        rows,
        comments=("# worker\tsource\tmodel\tminute\tscheduler\tdisplay_name",),
    )
    ROSTER.sync_roster_lock(rows, lock_path=lock_path)
    keep_models = [
        {"id": row["model"]}
        for row in rows
        if row["worker"] not in retire_workers and row["source"] == "nvidia"
    ]
    catalog.write_text(
        json.dumps(
            {
                "source": "synthetic-clustered-retirement",
                "providers": {
                    "opencode": [
                        {"id": "big-pickle", "cost": {"input": 0, "output": 0}},
                    ],
                    "nvidia": keep_models,
                    "openrouter": [{"id": "synthetic/unrelated:free"}],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return roster, lock_path, catalog, rows, retire_workers, retire_models


def test_rebalance_keeps_minutes_when_already_balanced() -> None:
    """A roster that already satisfies ±1 is left on its current minutes."""
    rows = [
        _row(str(index + 1), "nvidia", f"model-{index}", minute=str(minute))
        for index, minute in enumerate(ROSTER.SCHEDULE_MINUTES)
    ]

    rebalanced = ROSTER.rebalance_schedule_minutes(rows)

    assert [row["minute"] for row in rebalanced] == [row["minute"] for row in rows]
    assert [row["worker"] for row in rebalanced] == [row["worker"] for row in rows]
    assert ROSTER.schedule_is_balanced(rebalanced)


def test_rebalance_assigns_empty_minutes_on_already_balanced_roster() -> None:
    """An empty grown minute is assigned even when scheduled rows are ±1.

    Discovery runs #35-#37 appended ``minute=""`` and rebalance early-returned
    because the remaining scheduled buckets already looked balanced.
    """
    rows = [
        _row(str(index + 1), "nvidia", f"model-{index}", minute=str(minute))
        for index, minute in enumerate(ROSTER.SCHEDULE_MINUTES)
    ]
    rows.append(_row("99", "opencode-free", "ling-3.0-flash-fin-free", minute=""))

    assert not ROSTER.schedule_is_balanced(rows)

    rebalanced = ROSTER.rebalance_schedule_minutes(rows)
    grown = next(row for row in rebalanced if row["worker"] == "99")

    assert grown["minute"] != ""
    assert int(grown["minute"]) in ROSTER.SCHEDULE_MINUTES
    _assert_validator_minutes(rebalanced)


def test_rebalance_spreads_clustered_minutes() -> None:
    """Workers stacked on one minute are spread across the dispatcher grid."""
    rows = [
        _row(str(index + 1), "nvidia", f"model-{index}", minute="0")
        for index in range(len(ROSTER.SCHEDULE_MINUTES))
    ]

    rebalanced = ROSTER.rebalance_schedule_minutes(rows)

    assert {row["worker"] for row in rebalanced} == {row["worker"] for row in rows}
    assert ROSTER.schedule_is_balanced(rebalanced)
    assert {row["minute"] for row in rebalanced} == {
        str(minute) for minute in ROSTER.SCHEDULE_MINUTES
    }


def test_apply_rebalances_minutes_after_clustered_pin_retirement() -> None:
    """Removing clustered pins from a temp roster must leave minutes ±1.

    Replaces the live-TSV eight-pin assertion that broke once those production
    workers were retired. The synthetic roster still has eight clustered pins
    so apply has real work, then remaining minutes stay within ±1.
    """
    rows, retire_workers, _retire_models = _clustered_retirement_rows()
    raw_remaining = [row for row in rows if row["worker"] not in retire_workers]
    assert not ROSTER.schedule_is_balanced(raw_remaining)
    raw_counts = ROSTER.schedule_counts(raw_remaining)
    assert max(raw_counts.values()) - min(raw_counts.values()) > 1

    plan = _plan_for_workers(rows, set(retire_workers))
    assert {item.worker for item in plan.retirements} == set(retire_workers)

    remaining = RETIRE.apply_plan(rows, plan)
    remaining_ids = {int(row["worker"]) for row in remaining}

    assert remaining_ids == ROSTER.roster_worker_ids(rows) - {
        int(worker) for worker in retire_workers
    }
    assert ROSTER.schedule_is_balanced(remaining)
    counts = ROSTER.schedule_counts(remaining)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert set(counts) == set(ROSTER.SCHEDULE_MINUTES)


def test_apply_cli_rewrites_balanced_minutes_and_lock(tmp_path: Path) -> None:
    """CLI apply retires clustered pins, rebalances minutes, and syncs the lock."""
    roster, lock_path, catalog, _rows, retire_workers, retire_models = (
        _write_clustered_retirement_fixture(tmp_path)
    )

    status = RETIRE.run(
        [
            "apply",
            "--roster",
            str(roster),
            "--lock",
            str(lock_path),
            "--catalog-json",
            str(catalog),
            "--no-add-unused-free",
        ]
    )
    remaining = ROSTER.load_roster_rows(roster)
    lock = ROSTER.load_roster_lock(lock_path)

    assert status == 0
    assert {row["worker"] for row in remaining}.isdisjoint(retire_workers)
    assert ROSTER.schedule_is_balanced(remaining)
    assert set(lock["expected_workers"]) == ROSTER.roster_worker_ids(remaining)
    assert {int(worker) for worker in retire_workers}.issubset(set(lock["retired_workers"]))
    assert retire_models.issubset(set(lock["retired_models"]))


def test_apply_removes_dead_pins_and_syncs_expected_workers(tmp_path: Path) -> None:
    """Apply updates the TSV and generated expected-worker lock together."""
    roster = tmp_path / "free-model-factories.tsv"
    lock_path = tmp_path / "factory-expected-workers.json"
    roster.write_text(
        "\n".join(
            [
                "# worker\tsource\tmodel\tminute\tscheduler\tdisplay_name",
                "9\tnvidia\tstepfun-ai/step-3.7-flash\t5\tdispatcher\tStep",
                "39\topencode-free\tbig-pickle\t10\tdispatcher\tPickle",
                "41\topencode-free\tmimo-v2.5-free\t15\tdispatcher\tMiMo",
                "46\tkilo-auto\tkilo-auto/free\t40\tdispatcher\tKilo Auto Free · Forge",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "catalog-miss.json")
    rows = ROSTER.load_roster_rows(roster)
    plan = RETIRE.plan_retirement(rows, catalogs, add_unused_free=False)
    remaining = RETIRE.apply_plan(rows, plan)
    ROSTER.write_roster_rows(roster, remaining, ROSTER.load_roster_comments(roster))
    lock = ROSTER.sync_roster_lock(
        remaining,
        lock_path=lock_path,
        extra_retired_workers=(int(item.worker) for item in plan.retirements),
        extra_retired_models=(item.model for item in plan.retirements),
    )

    workers = {row["worker"] for row in ROSTER.load_roster_rows(roster)}
    assert workers == {"9", "39", "46"}
    assert 41 in lock["retired_workers"]
    assert "mimo-v2.5-free" in lock["retired_models"]
    assert set(lock["expected_workers"]) == {9, 39, 46}


def test_fixture_is_used_when_opencode_binary_is_missing() -> None:
    """CI injects a recorded catalog instead of calling a missing CLI."""
    catalogs = CATALOG.load_provider_catalogs(
        fixture_path=FIXTURES / "keep-present.json",
        require_cli=False,
        opencode_bin="/definitely/missing/opencode",
    )

    assert set(catalogs) == {"opencode", "nvidia", "openrouter"}
    assert catalogs["opencode"].source == "fixture"
    assert "big-pickle" in catalogs["opencode"].model_ids()


def test_verbose_cli_text_parses_cost_zero() -> None:
    """Verbose ``opencode models`` text attaches cost metadata to selectors."""
    text = "\n".join(
        [
            "opencode/big-pickle",
            "{",
            '  "id": "big-pickle",',
            '  "pricing": {"input": 0, "output": 0}',
            "}",
            "opencode/paid-frontier",
            "{",
            '  "id": "paid-frontier",',
            '  "cost": {"input": 2.5, "output": 2.5}',
            "}",
        ]
    )

    catalog = CATALOG.parse_opencode_models_text(text, "opencode")
    pickle = catalog.get("big-pickle")
    paid = catalog.get("paid-frontier")

    assert pickle is not None and pickle.cost_is_zero is True
    assert paid is not None and paid.cost_is_paid is True
    assert RETIRE.classify_opencode_free_eligibility("paid-frontier", paid) is False
    unnamed_free = CATALOG.CatalogEntry(
        "opencode",
        "zen-zero",
        cost_input=pickle.cost_input,
        cost_output=pickle.cost_output,
    )
    assert RETIRE.classify_opencode_free_eligibility("zen-zero", unnamed_free) is True


def test_cli_plan_fails_closed_without_catalog(tmp_path: Path) -> None:
    """A missing OpenCode catalog must not silently keep or drop pins."""
    roster = tmp_path / "roster.tsv"
    roster.write_text(
        "39\topencode-free\tbig-pickle\t5\tdispatcher\tPickle\n",
        encoding="utf-8",
    )

    status = RETIRE.run(
        [
            "plan",
            "--roster",
            str(roster),
            "--opencode-bin",
            "/definitely/missing/opencode",
        ]
    )

    assert status == 2


def test_opencode_free_display_name_matches_roster_style() -> None:
    """Converted unused-free pins get the existing OpenCode display-name shape."""
    assert ROSTER.opencode_free_display_name("mimo-v2.5-free") == (
        "OpenCode MiMo V2.5 Free"
    )
    assert ROSTER.opencode_free_display_name("mimo-v2.6-flash-free") == (
        "OpenCode MiMo V2.6 Flash Free"
    )
    assert ROSTER.opencode_free_display_name("ling-3.0-flash-fin-free") == (
        "OpenCode Ling 3.0 Flash Fin Free"
    )
    assert ROSTER.opencode_free_display_name("muse-spark-1.3-contributor-free") == (
        "OpenCode Muse Spark 1.3 Contributor Free"
    )
    assert ROSTER.opencode_free_display_name("nemotron-3.5-lightning-free") == (
        "OpenCode Nemotron 3.5 Lightning Free"
    )
    assert ROSTER.opencode_free_display_name("big-pickle") == "OpenCode Big Pickle"
    assert ROSTER.opencode_free_display_name("space-bunny-free") == (
        "OpenCode Space Bunny Free"
    )


def test_surplus_big_pickle_converts_highest_worker_first() -> None:
    """Lowest-numbered big-pickle stays reserved; highest surplus converts."""
    rows = [
        _row("23", "opencode-free", "big-pickle", minute="0"),
        _row("39", "opencode-free", "big-pickle", minute="5"),
        _row("59", "opencode-free", "big-pickle", minute="10"),
    ]

    surplus = ROSTER.surplus_big_pickle_rows(rows)

    assert [row["worker"] for row in surplus] == ["59", "39"]


def test_unused_free_converts_surplus_big_pickle_instead_of_growing() -> None:
    """Apply pins unused free models onto surplus big-pickle slots."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = [
        _row("23", "opencode-free", "big-pickle", minute="0"),
        _row("39", "opencode-free", "big-pickle", minute="5"),
        _row("41", "opencode-free", "mimo-v2.6-flash-free", minute="10"),
        _row("42", "opencode-free", "nemotron-3-ultra-free", minute="15"),
        _row("45", "opencode-free", "nemotron-3.5-lightning-free", minute="20"),
        _row("47", "opencode-free", "muse-spark-1.2-contributor-free", minute="25"),
        _row("49", "opencode-free", "space-bunny-free", minute="40"),
        _row("58", "opencode-free", "big-pickle", minute="30"),
        _row("59", "opencode-free", "big-pickle", minute="35"),
        *[
            _row(
                str(101 + index),
                "nvidia",
                "poolside/laguna-xs-2.1",
                minute=str(minute),
            )
            for index, minute in enumerate((45, 50, 55))
        ],
    ]

    plan = RETIRE.plan_retirement(rows, catalogs)
    added = {item.model: item for item in plan.additions}

    assert "ling-3.0-flash-fin-free" in added
    assert "muse-spark-1.3-contributor-free" in added
    assert "deepseek-v4-flash" not in added
    assert added["ling-3.0-flash-fin-free"].action == "convert"
    assert added["muse-spark-1.3-contributor-free"].action == "convert"
    assert {item.worker for item in plan.additions} <= {"58", "59"}
    assert all(item.previous_model == "big-pickle" for item in plan.additions)

    remaining = RETIRE.apply_plan(rows, plan)
    models = {row["model"] for row in remaining}
    workers = {row["worker"] for row in remaining}

    assert "ling-3.0-flash-fin-free" in models
    assert "muse-spark-1.3-contributor-free" in models
    assert workers == {row["worker"] for row in rows}
    assert sum(1 for row in remaining if row["model"] == "big-pickle") >= 1
    assert ROSTER.schedule_is_balanced(remaining)


def test_retired_lock_model_is_not_silently_re_pinned() -> None:
    """A live unused free model in retired_models stays off the TSV."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = [
        _row("23", "opencode-free", "big-pickle", minute="0"),
        _row("39", "opencode-free", "big-pickle", minute="5"),
        _row("41", "opencode-free", "mimo-v2.6-flash-free", minute="10"),
    ]
    lock = ROSTER.RosterLock(
        schema_version=1,
        expected_workers=[23, 39, 41],
        retired_workers=[],
        retired_models=["ling-3.0-flash-fin-free"],
    )

    plan = RETIRE.plan_retirement(rows, catalogs, lock=lock)
    added = {item.model for item in plan.additions}

    assert "ling-3.0-flash-fin-free" in {item.model for item in plan.unused_free}
    assert "ling-3.0-flash-fin-free" in plan.locked_unused
    assert "ling-3.0-flash-fin-free" not in added
    assert "muse-spark-1.3-contributor-free" in added


def test_mixed_retire_and_add_rebalances_in_one_plan() -> None:
    """One apply can drop a dead pin and convert unused free onto surplus."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = [
        _row("23", "opencode-free", "big-pickle", minute="0"),
        _row("39", "opencode-free", "big-pickle", minute="5"),
        _row("41", "opencode-free", "mimo-v2.6-flash-free", minute="10"),
        _row("49", "opencode-free", "space-bunny-free", minute="40"),
        _row("59", "opencode-free", "big-pickle", minute="15"),
        _row("80", "opencode-free", "absent-free-model", minute="20"),
        _row("201", "opencode-free", "muse-spark-1.2-contributor-free", minute="25"),
        _row("202", "opencode-free", "nemotron-3-ultra-free", minute="30"),
        _row("203", "opencode-free", "nemotron-3.5-lightning-free", minute="35"),
        *[
            _row(
                str(204 + index),
                "nvidia",
                "poolside/laguna-xs-2.1",
                minute=str(minute),
            )
            for index, minute in enumerate((45, 50, 55))
        ],
        _row("208", "nvidia", "poolside/laguna-xs-2.1", minute="0"),
    ]

    plan = RETIRE.plan_retirement(rows, catalogs)
    retired = {item.model for item in plan.retirements}
    added = {item.model: item for item in plan.additions}

    assert "absent-free-model" in retired
    assert "ling-3.0-flash-fin-free" in added
    assert added["ling-3.0-flash-fin-free"].action == "convert"

    remaining = RETIRE.apply_plan(rows, plan)
    models = {row["model"] for row in remaining}
    workers = {row["worker"] for row in remaining}

    assert "absent-free-model" not in models
    assert "80" not in workers
    assert "ling-3.0-flash-fin-free" in models
    assert "muse-spark-1.3-contributor-free" in models
    assert ROSTER.schedule_is_balanced(remaining)


def test_apply_grows_when_no_surplus_big_pickle_remains() -> None:
    """Without surplus big-pickle, unused free allocate a new worker id."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = [
        _row("39", "opencode-free", "big-pickle", minute="5"),
        _row("41", "opencode-free", "mimo-v2.6-flash-free", minute="10"),
    ]

    plan = RETIRE.plan_retirement(rows, catalogs)
    grow = [item for item in plan.additions if item.action == "add"]

    assert grow
    assert all(int(item.worker) > 41 for item in grow)
    assert "ling-3.0-flash-fin-free" in {item.model for item in grow}

    remaining = RETIRE.apply_plan(rows, plan)
    models = {row["model"] for row in remaining}

    assert "ling-3.0-flash-fin-free" in models
    assert "muse-spark-1.3-contributor-free" in models
    assert any(
        row["worker"] == "39" and row["model"] == "big-pickle" for row in remaining
    )
    for row in remaining:
        assert int(row["minute"]) in ROSTER.SCHEDULE_MINUTES
    assert "big-pickle" not in {item.model for item in grow}


def test_apply_does_not_grow_first_big_pickle_when_absent() -> None:
    """#2849: unused big-pickle is not grown onto a new worker id.

    Unique OpenCode frees already occupy the roster, so the only unused
    catalog-free id is ``big-pickle``. Discovery must not allocate worker
    72 (or any other new id) just to pin the first pickle.
    """
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = [
        _row("41", "opencode-free", "mimo-v2.6-flash-free", minute="5"),
        _row("42", "opencode-free", "nemotron-3-ultra-free", minute="10"),
        _row("45", "opencode-free", "nemotron-3.5-lightning-free", minute="15"),
        _row("46", "kilo-auto", "kilo-auto/free", minute="20"),
        _row("47", "opencode-free", "muse-spark-1.2-contributor-free", minute="25"),
        _row("49", "opencode-free", "space-bunny-free", minute="40"),
        _row("58", "opencode-free", "muse-spark-1.3-contributor-free", minute="30"),
        _row("59", "opencode-free", "ling-3.0-flash-fin-free", minute="35"),
    ]

    plan = RETIRE.plan_retirement(rows, catalogs)
    remaining = RETIRE.apply_plan(rows, plan)

    assert "big-pickle" in {item.model for item in plan.unused_free}
    assert plan.additions == ()
    assert not any(row["model"] == "big-pickle" for row in remaining)
    assert {row["worker"] for row in remaining} == {row["worker"] for row in rows}
    assert any(row["worker"] == "46" and row["model"] == "kilo-auto/free" for row in remaining)


def test_apply_grows_unique_frees_when_big_pickle_absent() -> None:
    """Unique unused frees still grow when the roster has no pickle pin."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = [
        _row("41", "opencode-free", "mimo-v2.6-flash-free", minute="10"),
        _row("46", "kilo-auto", "kilo-auto/free", minute="15"),
    ]

    plan = RETIRE.plan_retirement(rows, catalogs)
    grow = [item for item in plan.additions if item.action == "add"]
    remaining = RETIRE.apply_plan(rows, plan)

    assert grow
    assert "big-pickle" not in {item.model for item in plan.additions}
    assert "ling-3.0-flash-fin-free" in {item.model for item in grow}
    assert "muse-spark-1.3-contributor-free" in {item.model for item in grow}
    assert not any(row["model"] == "big-pickle" for row in remaining)
    assert any(row["worker"] == "46" and row["model"] == "kilo-auto/free" for row in remaining)
    for row in remaining:
        assert int(row["minute"]) in ROSTER.SCHEDULE_MINUTES


def _balanced_grow_roster() -> list[dict[str, str]]:
    """Return a ±1 roster with one big-pickle and one unused-free hole.

    All keep-present free OpenCode models except ``ling-3.0-flash-fin-free``
    are already pinned (including ``space-bunny-free``), so apply must grow
    a new worker rather than convert. Twelve slots cover every dispatcher
    minute so the validator loop applies.
    """
    return [
        _row("39", "opencode-free", "big-pickle", minute="0"),
        _row("41", "opencode-free", "mimo-v2.6-flash-free", minute="5"),
        _row("42", "opencode-free", "nemotron-3-ultra-free", minute="10"),
        _row("45", "opencode-free", "nemotron-3.5-lightning-free", minute="15"),
        _row("46", "kilo-auto", "kilo-auto/free", minute="20"),
        _row("47", "opencode-free", "muse-spark-1.2-contributor-free", minute="25"),
        _row("48", "opencode-free", "muse-spark-1.3-contributor-free", minute="30"),
        _row("49", "opencode-free", "space-bunny-free", minute="35"),
        *[
            _row(
                str(201 + index),
                "nvidia",
                "poolside/laguna-xs-2.1",
                minute=str(minute),
            )
            for index, minute in enumerate((40, 45, 50, 55))
        ],
    ]


def _balanced_mimo_upgrade_roster() -> list[dict[str, str]]:
    """Return a ±1 roster whose only catalog change is mimo-v2.5 → v2.6."""
    return [
        _row("39", "opencode-free", "big-pickle", minute="0"),
        _row("41", "opencode-free", "mimo-v2.5-free", minute="15"),
        _row("42", "opencode-free", "nemotron-3-ultra-free", minute="10"),
        _row("45", "opencode-free", "nemotron-3.5-lightning-free", minute="20"),
        _row("46", "kilo-auto", "kilo-auto/free", minute="25"),
        _row("47", "opencode-free", "muse-spark-1.2-contributor-free", minute="30"),
        _row("48", "opencode-free", "muse-spark-1.3-contributor-free", minute="35"),
        _row("49", "opencode-free", "ling-3.0-flash-fin-free", minute="40"),
        _row("50", "opencode-free", "space-bunny-free", minute="45"),
        *[
            _row(
                str(201 + index),
                "nvidia",
                "poolside/laguna-xs-2.1",
                minute=str(minute),
            )
            for index, minute in enumerate((5, 50, 55))
        ],
    ]


def test_apply_grow_assigns_minutes_on_balanced_roster(tmp_path: Path) -> None:
    """Grow-path apply writes a valid dispatcher minute the validator accepts.

    Reproduces discovery runs #35-#37: remaining scheduled rows already
    satisfy ±1, so a grown unused-free pin must not keep ``minute=""``.
    """
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = _balanced_grow_roster()
    assert ROSTER.schedule_is_balanced(rows)
    lock = ROSTER.RosterLock(
        schema_version=1,
        expected_workers=sorted(ROSTER.roster_worker_ids(rows)),
        retired_workers=[40, 43, 44],
        retired_models=[],
    )

    plan = RETIRE.plan_retirement(rows, catalogs, lock=lock)
    grow = [item for item in plan.additions if item.action == "add"]

    assert plan.retirements == ()
    assert {item.model for item in grow} == {"ling-3.0-flash-fin-free"}
    assert all(item.action == "add" for item in plan.additions)
    assert all(int(item.worker) > 48 for item in grow)

    roster = tmp_path / "free-model-factories.tsv"
    lock_path = tmp_path / "factory-expected-workers.json"
    ROSTER.write_roster_rows(roster, rows)
    ROSTER.write_roster_lock(lock_path, lock)

    status = RETIRE.run(
        [
            "apply",
            "--roster",
            str(roster),
            "--lock",
            str(lock_path),
            "--catalog-json",
            str(FIXTURES / "keep-present.json"),
        ]
    )
    remaining = ROSTER.load_roster_rows(roster)
    written_lock = ROSTER.load_roster_lock(lock_path)
    grown = [row for row in remaining if row["model"] == "ling-3.0-flash-fin-free"]

    assert status == 0
    assert grown
    assert all(row["minute"] != "" for row in grown)
    _assert_validator_minutes(remaining)
    assert set(written_lock["expected_workers"]) == ROSTER.roster_worker_ids(remaining)
    assert {40, 43, 44}.issubset(set(written_lock["retired_workers"]))
    assert any(row["worker"] == "46" and row["model"] == "kilo-auto/free" for row in remaining)


def test_same_plan_retire_and_add_reuses_freed_worker(tmp_path: Path) -> None:
    """Retire+add in one plan converts the freed slot instead of growing.

    The #37 mimo-v2.5-free → mimo-v2.6-flash-free case must keep worker 41
    and its minute even when the lock still lists 41 as expected.
    """
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    rows = _balanced_mimo_upgrade_roster()
    assert ROSTER.schedule_is_balanced(rows)
    lock = ROSTER.RosterLock(
        schema_version=1,
        expected_workers=sorted(ROSTER.roster_worker_ids(rows)),
        retired_workers=[40, 43, 44],
        retired_models=[],
    )

    plan = RETIRE.plan_retirement(rows, catalogs, lock=lock)
    added = {item.model: item for item in plan.additions}

    assert {item.model for item in plan.retirements} == {"mimo-v2.5-free"}
    assert added["mimo-v2.6-flash-free"].action == "convert"
    assert added["mimo-v2.6-flash-free"].worker == "41"
    assert added["mimo-v2.6-flash-free"].previous_model == "mimo-v2.5-free"
    assert "just-retired" in added["mimo-v2.6-flash-free"].reason
    assert all(item.action != "add" for item in plan.additions)

    roster = tmp_path / "free-model-factories.tsv"
    lock_path = tmp_path / "factory-expected-workers.json"
    ROSTER.write_roster_rows(roster, rows)
    ROSTER.write_roster_lock(lock_path, lock)

    status = RETIRE.run(
        [
            "apply",
            "--roster",
            str(roster),
            "--lock",
            str(lock_path),
            "--catalog-json",
            str(FIXTURES / "keep-present.json"),
        ]
    )
    remaining = ROSTER.load_roster_rows(roster)
    written_lock = ROSTER.load_roster_lock(lock_path)
    by_worker = {row["worker"]: row for row in remaining}

    assert status == 0
    assert "41" in by_worker
    assert by_worker["41"]["model"] == "mimo-v2.6-flash-free"
    assert by_worker["41"]["minute"] == "15"
    assert by_worker["41"]["source"] == "opencode-free"
    assert by_worker["46"]["model"] == "kilo-auto/free"
    assert "mimo-v2.5-free" not in {row["model"] for row in remaining}
    assert "xiaomi/mimo-v2.6-flash" not in {row["model"] for row in remaining}
    assert 41 in written_lock["expected_workers"]
    assert 41 not in written_lock["retired_workers"]
    assert "mimo-v2.5-free" in written_lock["retired_models"]
    assert {40, 43, 44}.issubset(set(written_lock["retired_workers"]))
    _assert_validator_minutes(remaining)


def test_apply_cli_converts_production_shaped_tsv_via_add_path(tmp_path: Path) -> None:
    """CLI apply pins missing live free models on a production-shaped TSV.

    Copies the committed roster, forces the two target models off it (back
    to big-pickle), then applies so the proof is the add path rather than a
    hand-edit. Safe after the committed TSV already contains those pins.
    """
    target = {"ling-3.0-flash-fin-free", "muse-spark-1.3-contributor-free"}
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    rewritten = []
    for row in rows:
        if row["model"] in target:
            rewritten.append(
                {
                    **row,
                    "model": "big-pickle",
                    "display_name": "OpenCode Big Pickle",
                }
            )
        else:
            rewritten.append(row)
    roster = tmp_path / "free-model-factories.tsv"
    lock_path = tmp_path / "factory-expected-workers.json"
    ROSTER.write_roster_rows(
        roster,
        rewritten,
        comments=ROSTER.load_roster_comments(
            ROOT / ".github" / "free-model-factories.tsv"
        ),
    )
    ROSTER.sync_roster_lock(rewritten, lock_path=lock_path)
    before = {row["model"] for row in ROSTER.load_roster_rows(roster)}
    assert before.isdisjoint(target)

    status = RETIRE.run(
        [
            "apply",
            "--roster",
            str(roster),
            "--lock",
            str(lock_path),
            "--catalog-json",
            str(FIXTURES / "keep-present.json"),
            "--retirement-comments",
            str(FIXTURES / "nvidia-410-comments.json"),
        ]
    )
    remaining = ROSTER.load_roster_rows(roster)
    models = {row["model"] for row in remaining}
    lock = ROSTER.load_roster_lock(lock_path)

    assert status == 0
    assert "ling-3.0-flash-fin-free" in models
    assert "muse-spark-1.3-contributor-free" in models
    assert "deepseek-v4-flash" not in models
    assert ROSTER.schedule_is_balanced(remaining)
    assert set(lock["expected_workers"]) == ROSTER.roster_worker_ids(remaining)
    assert sum(1 for row in remaining if row["model"] == "big-pickle") >= 1


def test_committed_tsv_pins_ling_and_muse_spark_13_via_add_path() -> None:
    """The factory TSV must pin the live unused free models this change adds."""
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    models = {row["model"] for row in rows}

    assert "ling-3.0-flash-fin-free" in models
    assert "muse-spark-1.3-contributor-free" in models
    assert "deepseek-v4-flash" not in models
    assert ROSTER.schedule_is_balanced(rows)


def test_committed_tsv_converts_nex_to_pickle_then_surplus_to_space_bunny_free() -> None:
    """Nex pins became pickles; one surplus pickle converted to Space Bunny.

    OpenRouter Nex N2.5 Pro/Mini free pins expired 2026-09-25 and were
    replaced in place by OpenCode Big Pickle on workers 48-49. Discovery
    then converted the highest surplus pickle (49) to unused
    ``space-bunny-free``. Worker 48 stays surplus pickle convert fodder.
    Worker 50 later retired when ``inclusionai/ling-3.0-flash-vl:free``
    left the live OpenRouter/OpenCode catalogs. Remaining worker ids and
    dispatcher minutes stay; kilo-auto 46 is untouched.
    ``deepseek-v4-flash-free`` stays lock-retired and is not re-pinned.
    """
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    lock = ROSTER.load_roster_lock(ROOT / ".github" / "factory-expected-workers.json")
    by_worker = {row["worker"]: row for row in rows}

    assert {48, 49}.issubset(set(lock["expected_workers"]))
    assert 50 not in lock["expected_workers"]
    assert {48, 49}.isdisjoint(set(lock["retired_workers"]))
    assert 50 in lock["retired_workers"]
    assert by_worker["46"]["source"] == "kilo-auto"
    assert by_worker["46"]["model"] == "kilo-auto/free"
    assert by_worker["48"] == {
        "worker": "48",
        "source": "opencode-free",
        "model": "big-pickle",
        "minute": "50",
        "scheduler": "dispatcher",
        "display_name": "OpenCode Big Pickle",
    }
    assert by_worker["49"] == {
        "worker": "49",
        "source": "opencode-free",
        "model": "space-bunny-free",
        "minute": "55",
        "scheduler": "dispatcher",
        "display_name": "OpenCode Space Bunny Free",
    }
    assert "50" not in by_worker
    assert "inclusionai/ling-3.0-flash-vl:free" not in {row["model"] for row in rows}
    assert "inclusionai/ling-3.0-flash-vl:free" in lock["retired_models"]
    assert "nex-agi/nex-n2.5-pro:free" not in {row["model"] for row in rows}
    assert "nex-agi/nex-n2.5-mini:free" not in {row["model"] for row in rows}
    assert "z-ai/glm-5.2:free" not in {row["model"] for row in rows}
    assert "xiaomi/mimo-v2.6-flash" not in {row["model"] for row in rows}
    assert "deepseek-v4-flash-free" not in {row["model"] for row in rows}
    assert "deepseek-v4-flash-free" in lock["retired_models"]
    assert 72 not in lock["expected_workers"]
    assert "72" not in by_worker
    assert ROSTER.opencode_model_is_free(by_worker["48"]["model"])
    assert ROSTER.opencode_model_is_free(by_worker["49"]["model"])
    assert ROSTER.opencode_free_display_name("big-pickle") == (
        by_worker["48"]["display_name"]
    )
    assert ROSTER.opencode_free_display_name("space-bunny-free") == (
        by_worker["49"]["display_name"]
    )
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    assert "big-pickle" in catalogs["opencode"].model_ids()
    assert "space-bunny-free" in catalogs["opencode"].model_ids()
    assert ROSTER.schedule_is_balanced(rows)
    assert sum(1 for row in rows if row["model"] == "big-pickle") == 1


def test_committed_tsv_discovery_apply_does_not_grow_first_pickle(
    tmp_path: Path,
) -> None:
    """Fixture apply on the converted roster is a no-op (no grow-72 pickle).

    Unique OpenCode frees including ``space-bunny-free`` are already pinned.
    The remaining surplus pickle on 48 stays convert fodder instead of
    discovery allocating a new worker.
    """
    source_roster = ROOT / ".github" / "free-model-factories.tsv"
    source_lock = ROOT / ".github" / "factory-expected-workers.json"
    roster = tmp_path / "free-model-factories.tsv"
    lock_path = tmp_path / "factory-expected-workers.json"
    roster.write_text(source_roster.read_text(encoding="utf-8"), encoding="utf-8")
    lock_path.write_text(source_lock.read_text(encoding="utf-8"), encoding="utf-8")
    before_rows = ROSTER.load_roster_rows(roster)
    before_lock = ROSTER.load_roster_lock(lock_path)
    before_workers = [row["worker"] for row in before_rows]

    status = RETIRE.run(
        [
            "apply",
            "--roster",
            str(roster),
            "--lock",
            str(lock_path),
            "--catalog-json",
            str(FIXTURES / "keep-present.json"),
            "--retirement-comments",
            str(FIXTURES / "nvidia-410-comments.json"),
        ]
    )
    remaining = ROSTER.load_roster_rows(roster)
    lock = ROSTER.load_roster_lock(lock_path)
    by_worker = {row["worker"]: row for row in remaining}

    assert status == 0
    assert [row["worker"] for row in remaining] == before_workers
    assert set(lock["expected_workers"]) == set(before_lock["expected_workers"])
    assert 72 not in lock["expected_workers"]
    assert "72" not in by_worker
    assert by_worker["46"]["model"] == "kilo-auto/free"
    assert by_worker["48"]["model"] == "big-pickle"
    assert by_worker["48"]["minute"] == "50"
    assert by_worker["49"]["model"] == "space-bunny-free"
    assert by_worker["49"]["minute"] == "55"
    assert by_worker["49"]["display_name"] == ROSTER.opencode_free_display_name(
        "space-bunny-free"
    )
    assert sum(1 for row in remaining if row["model"] == "big-pickle") == 1
    assert ROSTER.schedule_is_balanced(remaining)


def test_committed_tsv_converts_surplus_pickle_to_openrouter_nemotron_ultra_and_inkling() -> None:
    """Workers 51-53 stay expected and pin Harvy-smoked OpenRouter free models."""
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    lock = ROSTER.load_roster_lock(ROOT / ".github" / "factory-expected-workers.json")
    by_worker = {row["worker"]: row for row in rows}

    assert {51, 52, 53}.issubset(set(lock["expected_workers"]))
    assert {51, 52, 53}.isdisjoint(set(lock["retired_workers"]))
    assert by_worker["46"]["source"] == "kilo-auto"
    assert by_worker["46"]["model"] == "kilo-auto/free"
    assert by_worker["48"]["source"] == "opencode-free"
    assert by_worker["48"]["model"] == "big-pickle"
    assert by_worker["51"] == {
        "worker": "51",
        "source": "openrouter-free",
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "minute": "5",
        "scheduler": "dispatcher",
        "display_name": "OpenRouter Nemotron 3 Ultra Free",
    }
    assert by_worker["52"] == {
        "worker": "52",
        "source": "openrouter-free",
        "model": "thinkingmachines/inkling:free",
        "minute": "10",
        "scheduler": "dispatcher",
        "display_name": "OpenRouter Inkling Free",
    }
    assert by_worker["53"] == {
        "worker": "53",
        "source": "openrouter-free",
        "model": "thinkingmachines/inkling-small:free",
        "minute": "15",
        "scheduler": "dispatcher",
        "display_name": "OpenRouter Inkling Small Free",
    }
    assert by_worker["51"]["model"] not in lock["retired_models"]
    assert by_worker["52"]["model"] not in lock["retired_models"]
    assert by_worker["53"]["model"] not in lock["retired_models"]
    assert "nvidia/nemotron-3-ultra-550b-a55b" in lock["retired_models"]
    assert "thinkingmachines/inkling" in lock["retired_models"]
    assert ROSTER.openrouter_model_is_free(by_worker["51"]["model"])
    assert ROSTER.openrouter_model_is_free(by_worker["52"]["model"])
    assert ROSTER.openrouter_model_is_free(by_worker["53"]["model"])
    assert ROSTER.schedule_is_balanced(rows)


def test_committed_tsv_converts_surplus_pickle_to_z_ai_and_ollama_cloud() -> None:
    """Workers 54-55 stay expected and pin Harvy-smoked OpenAI-compat free models."""
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    lock = ROSTER.load_roster_lock(ROOT / ".github" / "factory-expected-workers.json")
    by_worker = {row["worker"]: row for row in rows}

    assert {54, 55}.issubset(set(lock["expected_workers"]))
    assert {54, 55}.isdisjoint(set(lock["retired_workers"]))
    assert by_worker["46"]["source"] == "kilo-auto"
    assert by_worker["46"]["model"] == "kilo-auto/free"
    assert by_worker["53"]["source"] == "openrouter-free"
    assert by_worker["54"] == {
        "worker": "54",
        "source": "z-ai",
        "model": "glm-4.5-flash",
        "minute": "20",
        "scheduler": "dispatcher",
        "display_name": "Z.AI GLM 4.5 Flash",
    }
    assert by_worker["55"] == {
        "worker": "55",
        "source": "ollama-cloud",
        "model": "nemotron-3-nano:30b",
        "minute": "25",
        "scheduler": "dispatcher",
        "display_name": "Ollama Cloud Nemotron 3 Nano 30B",
    }
    assert by_worker["54"]["model"] not in lock["retired_models"]
    assert by_worker["55"]["model"] not in lock["retired_models"]
    assert "z-ai/glm-4.5-flash" not in lock["retired_models"]
    assert "ollama-cloud/nemotron-3-nano:30b" not in lock["retired_models"]
    assert ROSTER.z_ai_model_is_free(by_worker["54"]["model"])
    assert ROSTER.ollama_cloud_model_is_free(by_worker["55"]["model"])
    assert not ROSTER.z_ai_model_is_free("glm-5")
    assert ROSTER.schedule_is_balanced(rows)


def test_committed_tsv_converts_surplus_pickle_to_openrouter_dots_note() -> None:
    """Worker 56 stays expected and pins Harvy-smoked OpenRouter Dots 3 Note Preview Free."""
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    lock = ROSTER.load_roster_lock(ROOT / ".github" / "factory-expected-workers.json")
    by_worker = {row["worker"]: row for row in rows}

    assert 56 in lock["expected_workers"]
    assert 56 not in lock["retired_workers"]
    assert by_worker["46"]["source"] == "kilo-auto"
    assert by_worker["46"]["model"] == "kilo-auto/free"
    assert by_worker["54"]["source"] == "z-ai"
    assert by_worker["54"]["model"] == "glm-4.5-flash"
    assert by_worker["55"]["source"] == "ollama-cloud"
    assert by_worker["55"]["model"] == "nemotron-3-nano:30b"
    assert by_worker["56"] == {
        "worker": "56",
        "source": "openrouter-free",
        "model": "dots-studio/dots-3-note-preview:free",
        "minute": "30",
        "scheduler": "dispatcher",
        "display_name": "OpenRouter Dots 3 Note Preview Free",
    }
    assert by_worker["56"]["model"] not in lock["retired_models"]
    assert "dots-studio/dots-3-note-preview:free" not in lock["retired_models"]
    assert ROSTER.openrouter_model_is_free(by_worker["56"]["model"])
    assert ROSTER.schedule_is_balanced(rows)


def test_stealth_union_alpha_lock_stays_consistent_with_roster() -> None:
    """Worker 23's stealth/union-alpha pin is either live or lock-retired.

    A live-TSV snapshot that required worker 23 to stay expected broke the
    next catalog apply (#2657). This contract holds before and after that
    pin disappears from ``opencode models openrouter``.
    """
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    lock = ROSTER.load_roster_lock(ROOT / ".github" / "factory-expected-workers.json")
    by_worker = {row["worker"]: row for row in rows}
    live = by_worker.get("23")

    if live is not None:
        assert live["source"] == "openrouter-free"
        assert live["model"] == "stealth/union-alpha"
        assert 23 in lock["expected_workers"]
        assert 23 not in lock["retired_workers"]
        assert "stealth/union-alpha" not in lock["retired_models"]
        assert not live["model"].endswith(":free")
        assert ROSTER.openrouter_model_is_free(live["model"])
    else:
        assert 23 not in lock["expected_workers"]
        assert 23 in lock["retired_workers"]
        assert "stealth/union-alpha" in lock["retired_models"]
    assert ROSTER.schedule_is_balanced(rows)


def test_committed_tsv_converts_surplus_pickle_to_openrouter_qwen38_27b() -> None:
    """Worker 29 stays expected and pins Harvy-listed OpenRouter Qwen3.8 27B Free."""
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    lock = ROSTER.load_roster_lock(ROOT / ".github" / "factory-expected-workers.json")
    by_worker = {row["worker"]: row for row in rows}

    assert 29 in lock["expected_workers"]
    assert 29 not in lock["retired_workers"]
    assert by_worker["21"]["source"] == "nvidia"
    assert by_worker["21"]["model"] == "google/gemma-4-31b-it"
    assert by_worker["46"]["source"] == "kilo-auto"
    assert by_worker["46"]["model"] == "kilo-auto/free"
    assert by_worker["54"]["source"] == "z-ai"
    assert by_worker["54"]["model"] == "glm-4.5-flash"
    assert by_worker["55"]["source"] == "ollama-cloud"
    assert by_worker["55"]["model"] == "nemotron-3-nano:30b"
    assert by_worker["56"]["source"] == "openrouter-free"
    assert by_worker["56"]["model"] == "dots-studio/dots-3-note-preview:free"
    assert "23" not in by_worker
    assert by_worker["29"] == {
        "worker": "29",
        "source": "openrouter-free",
        "model": "qwen/qwen3.8-27b:free",
        "minute": "0",
        "scheduler": "dispatcher",
        "display_name": "OpenRouter Qwen3.8 27B Free",
    }
    assert by_worker["29"]["model"] not in lock["retired_models"]
    assert "qwen/qwen3.8-27b:free" not in lock["retired_models"]
    assert by_worker["29"]["model"].endswith(":free")
    assert ROSTER.openrouter_model_is_free(by_worker["29"]["model"])
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    assert "qwen/qwen3.8-27b:free" in catalogs["openrouter"].model_ids()
    assert ROSTER.schedule_is_balanced(rows)
    assert sum(1 for row in rows if row["model"] == "big-pickle") == 1


def test_committed_tsv_upgrades_worker_41_to_opencode_mimo_v26_flash() -> None:
    """Worker 41 stays expected and pins OpenCode MiMo V2.6 Flash Free."""
    rows = ROSTER.load_roster_rows(ROOT / ".github" / "free-model-factories.tsv")
    lock = ROSTER.load_roster_lock(ROOT / ".github" / "factory-expected-workers.json")
    by_worker = {row["worker"]: row for row in rows}

    assert 41 in lock["expected_workers"]
    assert 41 not in lock["retired_workers"]
    assert by_worker["21"]["source"] == "nvidia"
    assert by_worker["21"]["model"] == "google/gemma-4-31b-it"
    assert by_worker["46"]["source"] == "kilo-auto"
    assert by_worker["46"]["model"] == "kilo-auto/free"
    assert by_worker["54"]["source"] == "z-ai"
    assert by_worker["54"]["model"] == "glm-4.5-flash"
    assert by_worker["55"]["source"] == "ollama-cloud"
    assert by_worker["55"]["model"] == "nemotron-3-nano:30b"
    assert by_worker["56"]["source"] == "openrouter-free"
    assert by_worker["56"]["model"] == "dots-studio/dots-3-note-preview:free"
    assert by_worker["41"] == {
        "worker": "41",
        "source": "opencode-free",
        "model": "mimo-v2.6-flash-free",
        "minute": "15",
        "scheduler": "dispatcher",
        "display_name": "OpenCode MiMo V2.6 Flash Free",
    }
    assert by_worker["41"]["model"] not in lock["retired_models"]
    assert "mimo-v2.6-flash-free" not in lock["retired_models"]
    assert by_worker["41"]["model"].endswith("-free")
    assert ROSTER.opencode_model_is_free(by_worker["41"]["model"])
    assert "xiaomi/mimo-v2.6-flash" not in {row["model"] for row in rows}
    assert not ROSTER.openrouter_model_is_free("xiaomi/mimo-v2.6-flash")
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "keep-present.json")
    assert "mimo-v2.6-flash-free" in catalogs["opencode"].model_ids()
    assert ROSTER.opencode_free_display_name("mimo-v2.6-flash-free") == (
        by_worker["41"]["display_name"]
    )
    assert ROSTER.schedule_is_balanced(rows)


def test_protected_openai_compat_pins_are_not_catalog_retired() -> None:
    """Z.AI and Ollama Cloud stay when OpenCode CLI catalogs omit them."""
    catalogs = CATALOG.load_catalog_fixture(FIXTURES / "catalog-miss.json")
    rows = [
        _row("54", "z-ai", "glm-4.5-flash"),
        _row("55", "ollama-cloud", "nemotron-3-nano:30b"),
        _row("46", "kilo-auto", "kilo-auto/free"),
    ]

    plan = RETIRE.plan_retirement(rows, catalogs)

    assert plan.retirements == ()
    by_model = {item.model: item for item in plan.kept}
    assert "not enumerated" in by_model["glm-4.5-flash"].reason
    assert "not enumerated" in by_model["nemotron-3-nano:30b"].reason
    assert "not enumerated" in by_model["kilo-auto/free"].reason
