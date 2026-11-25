#!/usr/bin/env python3
"""The repair-run context a duplicate closure left on the kept PR.

``factory_self_heal_duplicates`` closes a duplicate PR and leaves a trusted
comment on the kept PR naming the files only the closed PR changed. The repair
worker stages this module from trusted main and adds one line per closure to
its mission. Only ``github-actions[bot]`` comments count.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from factory_adopter import MARKER_PREFIX

MARKER = "factory-duplicate-closed-v1"
CONTEXT_RE = re.compile(rf"<!-- {MARKER_PREFIX}-{MARKER}:kept-(?P<kept>\d+):closed-(?P<closed>\d+) -->")
HEADLINE = "**Factory duplicate:**"


def marker(kept: int, closed: int) -> str:
    return f"<!-- {MARKER_PREFIX}-{MARKER}:kept-{kept}:closed-{closed} -->"


def context(records: Sequence[Mapping[str, Any]], pr: int) -> str:
    """One prompt line per duplicate closed in favor of ``pr``, from trusted comments."""
    import factory_review_policy as policy

    lines = []
    for body in policy.controller_comment_bodies(records):
        match = CONTEXT_RE.search(body)
        if match and int(match["kept"]) == pr and HEADLINE in body:
            lines.append(body.split(HEADLINE, 1)[1].strip().split("\n", 1)[0])
    return " ".join(f"Context: {line}" for line in lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("pr", type=int)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    proc = subprocess.run(
        ["gh", "api", "--paginate", "--slurp",
         f"repos/{args.repo}/issues/{args.pr}/comments?per_page=100"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    try:
        pages = json.loads(proc.stdout or "[]") if not proc.returncode else []
    except ValueError:
        pages = []
    rows = [row for page in pages if isinstance(page, list) for row in page if isinstance(row, dict)]
    text = context(rows, args.pr)
    if text:
        sys.stdout.write(f" {text}")
    return 0


if __name__ == "__main__":
    import factory_crash

    raise SystemExit(factory_crash.run(main))
