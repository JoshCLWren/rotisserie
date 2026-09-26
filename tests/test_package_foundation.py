"""Standalone package and distribution metadata invariants."""

from __future__ import annotations

import tomllib
from pathlib import Path

import rotisserie

ROOT = Path(__file__).parents[1]


def _project_metadata() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as pyproject:
        document = tomllib.load(pyproject)
    project = document["project"]
    assert isinstance(project, dict)
    return project


def test_package_version_matches_distribution_metadata() -> None:
    assert _project_metadata()["version"] == rotisserie.__version__


def test_distribution_declares_apache_license() -> None:
    assert _project_metadata()["license"] == "Apache-2.0"
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8").lstrip()
    assert license_text.startswith("Apache License")


def test_supported_python_floor_is_explicit() -> None:
    assert _project_metadata()["requires-python"] == ">=3.12"
