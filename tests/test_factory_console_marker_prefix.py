"""`rotisserie factory --repo` binds the runtime's comment-marker prefix to that repository."""

from __future__ import annotations

import io
import os

from rotisserie import cli
from rotisserie.factory_console import commands, runtime


def test_repo_flag_binds_the_marker_prefix_of_that_repository() -> None:
    arguments = cli.parser().parse_args(["factory", "--repo", "other/Gadget.Works", "status"])
    commands.main(arguments, env={"TERM": "dumb"}, stdout=io.StringIO(), executable="missing-gh")
    assert os.environ["FACTORY_MARKER_PREFIX"] == "gadget-works"
    marker = runtime.module("factory_review_policy").head_contributor_marker(
        pr=1, head="a" * 40, worker="2", epoch=3
    )
    assert marker.startswith("<!-- gadget-works-factory-head-contributor-v1:")
