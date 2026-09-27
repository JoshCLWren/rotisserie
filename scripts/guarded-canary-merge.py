#!/usr/bin/env python3
"""Manual entrypoint for the one-issue Rotisserie merge canary."""

from __future__ import annotations

import argparse
import os

from rotisserie.adapters.github.canary import CanaryScope, merge_canary, subprocess_runner


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    arguments = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    print(
        "canary dispatch: "
        f"repository={repository} pull_request={arguments.pull_request} "
        f"expected_head={arguments.expected_head}"
    )
    merge_canary(
        scope=CanaryScope(
            repository="JoshCLWren/rotisserie",
            issue=9,
            branch="rotisserie/canary-9",
        ),
        repository=repository,
        pull_request=arguments.pull_request,
        expected_head=arguments.expected_head,
        enabled=os.environ.get("ROTISSERIE_CANARY_ENABLED", "").lower() == "true",
        run=subprocess_runner,
    )


if __name__ == "__main__":
    main()
