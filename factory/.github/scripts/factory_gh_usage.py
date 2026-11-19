#!/usr/bin/env python3
"""Summarize one factory job's GitHub CLI requests from factory-gh-meter.sh.

Prints a single grep-able ``[factory-gh-usage]`` JSON line plus a Markdown
table for the job summary, so the shared installation REST budget can be
attributed to concrete callers instead of guessed.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def summarize(lines: list[str], top: int = 12) -> dict[str, int | list[tuple[str, int]]]:
    """Count requests by normalized command; paginated calls count once."""
    counts: Counter[str] = Counter()
    cache_hits = 0
    for line in lines:
        _, _, key = line.partition("\t")
        if key.strip() == "cache-hit":
            cache_hits += 1
        elif key.strip():
            counts[key.strip()] += 1
    writes = sum(n for key, n in counts.items()
                 if key.startswith("api ") and not key.startswith("api GET "))
    total = sum(counts.values())
    return {
        "total": total,
        "cache_hits": cache_hits,
        "github": total - cache_hits,
        "writes": writes,
        "paginated": sum(n for key, n in counts.items() if key.endswith(" paginate")),
        "top": counts.most_common(top),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--label", default="job")
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    try:
        lines = args.log.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    usage = summarize(lines)
    print(f"[factory-gh-usage] {json.dumps({'label': args.label, **usage}, sort_keys=True)}")
    if args.summary:
        top_rows = usage["top"]
        rows = "\n".join(
            f"| `{key}` | {count} |" for key, count in (top_rows if isinstance(top_rows, list) else [])
        )
        args.summary.open("a", encoding="utf-8").write(
            f"### GitHub CLI requests ({args.label})\n\n"
            f"Total `gh` invocations: {usage['total']}; reached GitHub: {usage['github']} "
            f"(cache hits {usage['cache_hits']}, writes {usage['writes']}, "
            f"paginated {usage['paginated']}; each paginated call may be several requests)\n\n"
            f"| Command | Calls |\n| --- | ---: |\n{rows}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
