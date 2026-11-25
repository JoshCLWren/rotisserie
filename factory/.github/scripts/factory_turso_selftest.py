"""Run the libSQL store test matrix against a configured server and publish a JSON summary.

Trusted Actions only. The suite writes only rows of repositories named
``<namespace>/...``; this runner deletes every such row afterwards, even when
a test fails. The summary holds counts and usage, never the URL or token.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime
from pathlib import Path

import factory_libsql as libsql

FACTORY = Path(__file__).resolve().parents[2]
SUITE = (
    "tests/test_factory_sqlite_schema.py",
    "tests/test_turso_concurrency.py",
    "tests/test_turso_lifecycle.py",
    "tests/test_turso_faults.py",
)
PREFIX = "selftest-"
NAMESPACE = re.compile(r"^selftest-[A-Za-z0-9_.-]{1,50}$")


def _counts(report: Path) -> dict[str, int]:
    found = {"passed": 0, "failed": 0, "skipped": 0}
    if not report.exists():
        return found
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            found["failed"] += 1
        elif case.find("skipped") is not None:
            found["skipped"] += 1
        else:
            found["passed"] += 1
    return found


def _usage(path: Path) -> dict[str, int]:
    total = {"requests": 0, "rows_read": 0, "rows_written": 0}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            for key, value in json.loads(line).items():
                total[key] = total.get(key, 0) + int(value)
    return total


def sweep(store: libsql.Store) -> int:
    """Delete every row any self-test run left, including a cancelled earlier run's.

    Runs share one concurrency group, so no other self-test is live. Test event
    IDs are fixed and ``event_id`` is unique across repositories, so a stale
    row from a killed run would otherwise turn a later insert into a no-op.
    """
    from factory_turso_backup import TABLES

    results = store.batch(
        [
            (
                f"DELETE FROM {table} WHERE substr(repo, 1, :n) = :prefix RETURNING 1 AS x",
                {"n": len(PREFIX), "prefix": PREFIX},
            )
            for table in TABLES
        ]
    )
    return sum(len(rows) for rows in results)


def _store() -> libsql.Store:
    token_file = os.environ.get("FACTORY_TEST_LIBSQL_TOKEN_FILE")
    token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else ""
    return libsql.HttpStore(os.environ["FACTORY_TEST_LIBSQL_URL"], token)


def run(namespace: str, *, measure: bool) -> dict[str, object]:
    if not NAMESPACE.fullmatch(namespace):
        raise ValueError("invalid self-test namespace")
    if not os.environ.get("FACTORY_TEST_LIBSQL_URL"):
        raise RuntimeError("FACTORY_TEST_LIBSQL_URL is required")
    work = Path(tempfile.mkdtemp(prefix="factory-selftest-"))
    env = {
        **os.environ,
        "FACTORY_TEST_NAMESPACE": namespace,
        "FACTORY_TEST_USAGE_FILE": str(work / "usage.jsonl"),
    }
    report = work / "junit.xml"
    store = _store()
    libsql.ensure_schema(store)
    stale = sweep(store)
    started = datetime.now(UTC)
    tests = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            *SUITE,
        ],
        cwd=FACTORY,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=1500,
    )
    summary: dict[str, object] = {
        "mode": "selftest",
        "schema": 1,
        "started_at": started.isoformat(),
        **_counts(report),
        "pytest_exit": tests.returncode,
        "tail": tests.stdout[-2000:],
        "stale_rows_deleted_first": stale,
    }
    if measure:
        measured = subprocess.run(
            [sys.executable, "-m", "tests.turso_usage"],
            cwd=FACTORY,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
        try:
            summary["measurement"] = json.loads(measured.stdout)
        except ValueError:
            summary["measurement"] = {"error": measured.stderr[-500:]}
    summary["leftover_rows_deleted"] = sweep(_store())
    summary["usage"] = _usage(work / "usage.jsonl")
    failed = tests.returncode != 0 or summary["failed"] or not summary["passed"]
    summary["status"] = "failed" if failed else "passed"
    summary["finished_at"] = datetime.now(UTC).isoformat()
    return summary


def crashed(exc: BaseException) -> dict[str, object]:
    """A summary for a run that stopped before its tests; never carries the store URL."""
    message = f"{type(exc).__name__}: {exc}"
    url = os.environ.get("FACTORY_TEST_LIBSQL_URL", "")
    if url:
        message = message.replace(url, "<store>")
    return {
        "mode": "selftest",
        "schema": 1,
        "status": "failed",
        "error": message[:500],
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "finished_at": datetime.now(UTC).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--measure", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run(args.namespace, measure=args.measure)
    except Exception as exc:
        summary = crashed(exc)
    args.out.write_text(json.dumps(summary, sort_keys=True, indent=2), encoding="utf-8")
    print(
        f"[factory-store] self-test {summary['status']}: {summary['passed']} passed, "
        f"{summary['failed']} failed, {summary['skipped']} skipped"
    )
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    sys.exit(main())
