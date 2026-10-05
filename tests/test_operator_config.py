from __future__ import annotations

from pathlib import Path

import pytest

from rotisserie.domain import WorkerId
from rotisserie.operator import ConfigurationError, load_config

BASE = """schema_version = 1
[repository]
host = "example.test"
owner = "rotisserie"
name = "demo"
[repositories]
allow = [{ host = "example.test", owner = "rotisserie", name = "demo" }]
[local]
snapshot = "graph.json"
state_directory = "state"
mutations_enabled = false
"""


def write_config(tmp_path: Path, workers: str = "") -> Path:
    config = tmp_path / "config.toml"
    config.write_text(BASE + workers, encoding="utf-8")
    return config


def test_config_without_workers_table_has_no_inventory(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    assert config.workers is None


def test_workers_table_parses_string_and_table_entries(tmp_path: Path) -> None:
    workers = """
[workers]
expected = ["model:alpha", "model:beta", { namespace = "human", value = "lead" }]
scheduled = ["model:alpha"]
retired = ["model:gamma"]
"""
    config = load_config(write_config(tmp_path, workers))
    assert config.workers is not None
    inventory = config.workers
    assert inventory.size == 3
    assert inventory.is_expected(WorkerId("model", "alpha"))
    assert inventory.is_expected(WorkerId("human", "lead"))
    assert inventory.is_scheduled(WorkerId("model", "alpha"))
    assert inventory.is_retired(WorkerId("model", "gamma"))
    assert not inventory.is_expected(WorkerId("model", "gamma"))


def test_empty_retired_array_is_allowed(tmp_path: Path) -> None:
    workers = """
[workers]
expected = ["model:alpha"]
retired = []
"""
    config = load_config(write_config(tmp_path, workers))
    assert config.workers is not None
    assert config.workers.retired == frozenset()


def test_missing_expected_is_rejected(tmp_path: Path) -> None:
    workers = """
[workers]
scheduled = ["model:alpha"]
"""
    with pytest.raises(ConfigurationError, match="workers.expected must be a non-empty array"):
        load_config(write_config(tmp_path, workers))


def test_empty_expected_array_is_rejected(tmp_path: Path) -> None:
    workers = """
[workers]
expected = []
"""
    with pytest.raises(ConfigurationError, match="workers.expected must be a non-empty array"):
        load_config(write_config(tmp_path, workers))


def test_duplicate_expected_identity_is_rejected(tmp_path: Path) -> None:
    workers = """
[workers]
expected = ["model:alpha", "model:alpha"]
"""
    with pytest.raises(ConfigurationError, match="duplicate worker identities"):
        load_config(write_config(tmp_path, workers))


def test_retired_worker_in_expected_is_rejected(tmp_path: Path) -> None:
    workers = """
[workers]
expected = ["model:alpha"]
retired = ["model:alpha"]
"""
    with pytest.raises(ConfigurationError, match="both expected and retired"):
        load_config(write_config(tmp_path, workers))


def test_scheduled_outside_expected_is_rejected(tmp_path: Path) -> None:
    workers = """
[workers]
expected = ["model:alpha"]
scheduled = ["model:beta"]
"""
    with pytest.raises(ConfigurationError, match="subset of expected"):
        load_config(write_config(tmp_path, workers))


def test_string_entry_without_namespace_separator_is_rejected(tmp_path: Path) -> None:
    workers = """
[workers]
expected = ["alpha"]
"""
    with pytest.raises(ConfigurationError, match="namespace:value form"):
        load_config(write_config(tmp_path, workers))


def test_non_string_entry_is_rejected(tmp_path: Path) -> None:
    workers = """
[workers]
expected = [12]
"""
    with pytest.raises(ConfigurationError, match="strings or tables"):
        load_config(write_config(tmp_path, workers))


def test_table_entry_missing_value_is_rejected(tmp_path: Path) -> None:
    workers = """
[workers]
expected = [{ namespace = "model" }]
"""
    with pytest.raises(ConfigurationError, match="string worker namespace and value"):
        load_config(write_config(tmp_path, workers))


def test_workers_must_be_a_table(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('workers = "nope"\n' + BASE, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="workers must be a table"):
        load_config(config)
