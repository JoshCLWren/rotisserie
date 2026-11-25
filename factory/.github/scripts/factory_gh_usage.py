#!/usr/bin/env python3
"""Summarize one factory job's GitHub CLI requests from factory-gh-meter.sh.

Prints a single grep-able ``[factory-gh-usage]`` JSON line plus a Markdown
table for the job summary, so the shared installation REST budget can be
attributed to concrete callers instead of guessed. ``--record`` also keeps the
last jobs of each kind in one store row, and ``export`` prints those rows for
the published ``factory-stats.json``, where ``baste recap`` and ``baste
limits`` read them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

KINDS = ("dispatch", "merge")
KEEP = 12


def summarize(lines: list[str], top: int = 12) -> dict[str, Any]:
    """Count requests by normalized command; paginated calls count once."""
    counts: Counter[str] = Counter()
    buckets: Counter[str] = Counter()
    cache_hits = 0
    for line in lines:
        fields = line.split("\t")
        key = fields[1].strip() if len(fields) > 1 else ""
        if key == "cache-hit":
            cache_hits += 1
        elif key:
            counts[key] += 1
            bucket = fields[2].strip() if len(fields) > 2 else ""
            buckets[bucket or "workflow"] += 1
    # Cache hits are reads, so they are logged in the reader bucket when it is in use.
    hit_bucket = "reader" if buckets["reader"] >= cache_hits else "workflow"
    buckets[hit_bucket] = max(0, buckets[hit_bucket] - cache_hits)
    writes = sum(
        n for key, n in counts.items() if key.startswith("api ") and not key.startswith("api GET ")
    )
    total = sum(counts.values())
    return {
        "total": total,
        "cache_hits": cache_hits,
        "github": total - cache_hits,
        "writes": writes,
        "paginated": sum(n for key, n in counts.items() if key.endswith(" paginate")),
        "buckets": {"workflow": buckets["workflow"], "reader": buckets["reader"]},
        "top": counts.most_common(top),
    }


def budget(path: Path | None) -> dict[str, int] | None:
    """The workflow token's REST bucket from a saved ``gh api rate_limit`` reply."""
    if path is None:
        return None
    try:
        core = json.loads(path.read_text(encoding="utf-8"))["resources"]["core"]
        values = {key: int(core[key]) for key in ("remaining", "limit", "reset")}
    except OSError, ValueError, KeyError, TypeError:
        return None
    return values if values["limit"] > 0 else None


def kind_of(label: str) -> str:
    return label.split(" ", 1)[0]


def record(repo: str, label: str, usage: dict[str, Any], rest: dict[str, int] | None) -> None:
    """Append this job to its kind's row; advisory, so store errors are only printed."""
    kind = kind_of(label)
    if kind not in KINDS:
        return
    try:
        import factory_turso_core as core
        import factory_turso_cursors as cursors

        if not core.enabled():
            return
        now = int(time.time())
        found = cursors.get(repo, f"gh-usage:{kind}")
        jobs = list((found[1] if found else {}).get("jobs") or [])[-(KEEP - 1) :]
        fields = ("github", "cache_hits", "writes", "paginated", "buckets")
        job = {key: usage[key] for key in fields}
        jobs.append({"at": now, "label": label, **job, "top": usage["top"][:5], "budget": rest})
        at = max(now, found[0] + 1) if found else now
        cursors.advance(repo, f"gh-usage:{kind}", at, {"jobs": jobs})
    except Exception as exc:
        print(f"[factory-gh-usage] not recorded ({type(exc).__name__})", file=sys.stderr)


def export(repo: str) -> dict[str, Any]:
    import factory_turso_cursors as cursors

    found = {kind: cursors.get(repo, f"gh-usage:{kind}") for kind in KINDS}
    return {kind: row[1].get("jobs") or [] for kind, row in found.items() if row}


def _write_summary(
    path: Path, label: str, usage: dict[str, Any], rest: dict[str, int] | None
) -> None:
    top_rows = usage["top"]
    rows = "\n".join(
        f"| `{key}` | {count} |" for key, count in (top_rows if isinstance(top_rows, list) else [])
    )
    left = f"; workflow token budget {rest['remaining']}/{rest['limit']}" if rest else ""
    path.open("a", encoding="utf-8").write(
        f"### GitHub CLI requests ({label})\n\n"
        f"Total `gh` invocations: {usage['total']}; reached GitHub: {usage['github']} "
        f"(cache hits {usage['cache_hits']}, writes {usage['writes']}, "
        f"paginated {usage['paginated']}; each paginated call may be several requests; "
        f"workflow token {usage['buckets']['workflow']}, read-only App "
        f"{usage['buckets']['reader']}{left})\n\n"
        f"| Command | Calls |\n| --- | ---: |\n{rows}\n"
    )


def main() -> int:
    if sys.argv[1:2] == ["export"]:
        exporter = argparse.ArgumentParser(description="print the recorded job usage rows")
        exporter.add_argument("export")
        exporter.add_argument("--repo", required=True)
        print(json.dumps(export(exporter.parse_args().repo), sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--label", default="job")
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--rate-limit", type=Path)
    parser.add_argument("--record", metavar="REPO")
    args = parser.parse_args()
    try:
        lines = args.log.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    usage = summarize(lines)
    rest = budget(args.rate_limit)
    line = json.dumps({"label": args.label, **usage, "budget": rest}, sort_keys=True)
    print(f"[factory-gh-usage] {line}")
    if args.record:
        record(args.record, args.label, usage, rest)
    if args.summary:
        _write_summary(args.summary, args.label, usage, rest)
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    sys.exit(main())
