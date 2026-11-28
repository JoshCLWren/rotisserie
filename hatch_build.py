"""Add the Cargo extension to Hatch's normal and editable wheels."""

from __future__ import annotations

from pathlib import Path
from runpy import run_path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from packaging.tags import sys_tags


class CustomBuildHook(BuildHookInterface):  # type: ignore[misc]
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel":
            return
        builder = run_path(str(Path(self.root) / "factory/native/build_extension.py"))
        artifact = str(builder["build"]())
        filename = builder["extension_filename"]()
        include = "force_include_editable" if version == "editable" else "force_include"
        build_data[include][artifact] = filename
        build_data["pure_python"] = False
        if version != "editable":
            platform = next(
                tag.platform
                for tag in sys_tags()
                if not tag.platform.startswith(("manylinux", "musllinux"))
            )
            build_data["tag"] = f"cp314-abi3-{platform}"
