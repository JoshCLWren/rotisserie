"""Same-run exclusion of a proven exhausted credit pool (issue #234).

Catalogs are synthetic and copy the shape of a real OrcaRouter /models
response: string prices, request-priced rows, tiered rows, one all-zero row
and rows that publish no price at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from factory_billing_pool import (  # noqa: E402
    CREDIT_POOL,
    billing_class,
    credit_pool_members,
    exhausted_pool,
    membership,
)
from factory_model_failover import ExhaustedCandidatesError, select_next_fleet  # noqa: E402

OUT_OF_CREDITS = (
    "Error: You\u2019re out of credits \u2014 this request needs $0.4213. Add credits to keep going\n"
)
PAID_COUNT = 150
FREE = ("synthetic/zero-token-coder", "synthetic/zero-request-coder")
UNKNOWN = ("synthetic/router-auto", "synthetic/unpriced-coder")


def _paid(index: int) -> dict:
    row: dict = {"id": f"synthetic/paid-{index:03d}", "supported_parameters": ["tools"]}
    if index % 3 == 0:
        row["pricing"] = {"request": "0.150000"}
    elif index % 3 == 1:
        row["pricing"] = {
            "prompt": "0.0000020000",
            "completion": "0.0000120000",
            "tiers": [
                {"max_input_tokens": 0, "prompt": "0.0000040000", "completion": "0.0000180000"}
            ],
        }
    else:
        row["pricing"] = {"prompt": "0.0000100000", "completion": "0.0000500000"}
    return row


def orca_catalog() -> dict:
    rows = [_paid(i) for i in range(PAID_COUNT)]
    rows.append({"id": FREE[0], "pricing": {"prompt": "0", "completion": "0.000000"}})
    rows.append({"id": FREE[1], "pricing": {"request": "0.000000"}})
    rows.extend({"id": model, "owned_by": "synthetic"} for model in UNKNOWN)
    return {"data": rows}


def other_catalog() -> dict:
    return {"data": [{"id": "independent/coder", "pricing": {"prompt": "1", "completion": "2"}}]}


@pytest.mark.parametrize(
    "pricing,expected",
    [
        ({"prompt": "0.0000100000", "completion": "0.0000500000"}, "paid"),
        ({"request": "0.240000"}, "paid"),
        (
            {
                "prompt": "0",
                "completion": "0",
                "tiers": [{"prompt": "0.000004", "completion": "0"}],
            },
            "paid",
        ),
        ({"prompt": "0", "completion": "0.000000"}, "free"),
        ({"request": "0.000000"}, "free"),
        ({"prompt": "-1", "completion": "-1"}, "unknown"),
        ({"prompt": "n/a", "completion": "0"}, "unknown"),
        ({"input_cache_read": "0.1"}, "unknown"),
        (None, "unknown"),
    ],
)
def test_billing_class_comes_only_from_published_price(pricing, expected) -> None:
    row = {"id": "synthetic/model:free-suffix-is-not-evidence"}
    if pricing is not None:
        row["pricing"] = pricing
    assert billing_class(row) == expected


def test_membership_ignores_names_and_non_catalog_snapshots() -> None:
    catalog = orca_catalog()
    kinds = membership(catalog)
    assert {m for m, k in kinds.items() if k == "free"} == set(FREE)
    assert {m for m, k in kinds.items() if k == "unknown"} == set(UNKNOWN)
    assert len(credit_pool_members(catalog)) == PAID_COUNT
    assert credit_pool_members(json.dumps(catalog)) == credit_pool_members(catalog)
    assert credit_pool_members("kilo/auto\nkilo/other\n") == frozenset()
    assert credit_pool_members({"opencode-go": {"models": {}}}) == frozenset()


def test_only_an_attested_empty_balance_on_a_proven_member_trips_the_pool() -> None:
    catalog = orca_catalog()
    paid = "synthetic/paid-002"
    assert exhausted_pool(paid, catalog, OUT_OF_CREDITS) == CREDIT_POOL
    assert exhausted_pool(paid, catalog, "Your account balance exhausted") == CREDIT_POOL
    assert exhausted_pool(paid, catalog, "insufficient balance for this request") is None
    assert exhausted_pool(paid, catalog, "HTTP 429 Too Many Requests") is None
    assert exhausted_pool(paid, catalog, "rate limit exceeded: free-models-per-day") is None
    assert exhausted_pool(FREE[0], catalog, OUT_OF_CREDITS) is None
    assert exhausted_pool(UNKNOWN[0], catalog, OUT_OF_CREDITS) is None
    assert exhausted_pool("synthetic/not-listed", catalog, OUT_OF_CREDITS) is None


def _select(attempted: set, pools: frozenset, catalogs: dict | None = None) -> dict:
    catalogs = catalogs or {"orcarouter": orca_catalog(), "nvidia": other_catalog()}
    return select_next_fleet(
        "77",
        "orcarouter",
        attempted,
        set(),
        {},
        catalogs,
        set(catalogs),
        proven=set(),
        exhausted_credit_pools=pools,
    )


def test_exhausted_paid_pool_leaves_free_and_unknown_selectable() -> None:
    attempted = {("orcarouter", "synthetic/paid-001")}
    seen = set()
    while True:
        pick = _select(attempted, frozenset({"orcarouter"}))
        attempted.add((pick["source"], pick["model"]))
        if pick["source"] != "orcarouter":
            break
        seen.add(pick["model"])
    assert seen == set(FREE) | set(UNKNOWN)
    assert pick == {**pick, "source": "nvidia", "model": "independent/coder"}


def test_exhausted_free_allowance_does_not_suppress_paid_candidates() -> None:
    catalog = orca_catalog()
    refusal = "Free tier: rate limit exceeded: free-models-per-day"
    assert exhausted_pool(FREE[0], catalog, refusal) is None
    assert exhausted_pool(FREE[0], catalog, OUT_OF_CREDITS) is None
    attempted = {("orcarouter", FREE[0])}
    pick = _select(attempted, frozenset())
    assert pick["source"] == "orcarouter"
    assert pick["model"] != FREE[0]


def test_pool_exclusion_never_touches_another_source() -> None:
    catalogs = {"orcarouter": orca_catalog(), "nvidia": orca_catalog()}
    attempted = {("orcarouter", m) for m in (*FREE, *UNKNOWN)}
    pick = _select(attempted, frozenset({"orcarouter"}), catalogs)
    assert pick["source"] == "nvidia"
    assert pick["model"].startswith("synthetic/paid-")


def _carousel(trip: bool) -> list[tuple[str, str]]:
    """Every OrcaRouter request is refused; count requests before leaving it."""
    catalogs = {"orcarouter": orca_catalog(), "nvidia": other_catalog()}
    attempted: set[tuple[str, str]] = set()
    pools: set[str] = set()
    orca: list[tuple[str, str]] = []
    while True:
        try:
            pick = _select(attempted, frozenset(pools), catalogs)
        except ExhaustedCandidatesError:
            return orca
        pair = (pick["source"], pick["model"])
        attempted.add(pair)
        if pick["source"] != "orcarouter":
            return orca
        orca.append(pair)
        if trip and exhausted_pool(pick["model"], catalogs["orcarouter"], OUT_OF_CREDITS):
            pools.add("orcarouter")


def test_regression_orcarouter_out_of_credits_carousel_is_bounded() -> None:
    """ComicPile run 45300344438 sent 153 out-of-credit requests to OrcaRouter."""
    assert len(_carousel(trip=False)) == PAID_COUNT + len(FREE) + len(UNKNOWN)
    bounded = _carousel(trip=True)
    paid = [m for _, m in bounded if m.startswith("synthetic/paid-")]
    assert len(paid) == 1
    assert len(bounded) <= 1 + len(FREE) + len(UNKNOWN)


def test_failover_cli_applies_the_exhausted_pool_argument(tmp_path: Path) -> None:
    cache = tmp_path / "fleet.json"
    cache.write_text(json.dumps({"orcarouter": orca_catalog(), "nvidia": other_catalog()}))
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text("{}")
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps(["orcarouter", "nvidia"]))
    attempted = [json.dumps(["orcarouter", m]) for m in (*FREE, *UNKNOWN)]

    def pick(*extra: str) -> dict:
        args = [
            sys.executable,
            str(SCRIPTS / "factory_model_failover.py"),
            "--source",
            "orcarouter",
            "--worker",
            "77",
            "--exclusions-file",
            str(exclusions),
            "--eligible-sources-file",
            str(sources),
            "--fleet-catalog-cache",
            str(cache),
            *extra,
        ]
        for pair in attempted:
            args += ["--attempted-pair", pair]
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=True,
            env={"FACTORY_CANDIDATE_HEALTH": "", "PATH": "/usr/bin:/bin"},
        )
        return json.loads(result.stdout)

    assert pick()["source"] == "orcarouter"
    assert pick("--exhausted-credit-pool", "orcarouter")["source"] == "nvidia"
