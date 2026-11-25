#!/usr/bin/env python3
"""Which open factory PR heads have no CI or repair-guard run yet.

Reads ``sha<TAB>pr<TAB>branch`` lines on stdin and prints
``pr<TAB>branch<TAB>workflow name`` for each run that is missing. A head that
has a run keeps it, so that fact is kept in the store by SHA and the drain
asks GitHub only about heads it has not seen with a run. A failed count
dispatches nothing; the next drain asks again.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from factory_head_memo import HeadMemo  # noqa: E402

WORKFLOWS = (("ci.yml", "CI"), ("fixed-model-pr-repair-guard.yml", "Fixed Model PR Repair Guard"))
MEMO = "drain-run-heads"


def run_count(repo: str, workflow: str, sha: str) -> int | None:
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/workflows/{workflow}/runs?head_sha={sha}&per_page=1",
            "--jq",
            ".total_count",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    value = proc.stdout.strip()
    return int(value) if proc.returncode == 0 and value.isdecimal() else None


def missing(
    repo: str, heads: list[tuple[str, str, str]], memo: HeadMemo
) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for sha, pr, branch in heads:
        seen = set(memo.get(sha) or [])
        for workflow, name in WORKFLOWS:
            if workflow in seen:
                continue
            count = run_count(repo, workflow, sha)
            if count:
                seen.add(workflow)
            elif count == 0:
                out.append((pr, branch, name))
        if seen:
            memo.put(sha, sorted(seen))
    return out


def main() -> int:
    from factory_adopter import repository

    repo = repository()
    heads = [
        (parts[0], parts[1], parts[2])
        for line in sys.stdin.read().splitlines()
        if len(parts := line.split("\t")) == 3 and parts[0]
    ]
    memo = HeadMemo(repo, MEMO)
    for pr, branch, name in missing(repo, heads, memo):
        print(f"{pr}\t{branch}\t{name}")
    memo.save({sha for sha, _, _ in heads})
    print(
        json.dumps({"heads": len(heads), "known": sum(1 for s, _, _ in heads if memo.get(s))}),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    raise SystemExit(main())
