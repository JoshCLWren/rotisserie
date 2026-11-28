#!/usr/bin/env python3
"""Measure complete, offline factory policy and console paths on fixed fixtures."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any


def offline_guard(event: str, _args: tuple[object, ...]) -> None:
    if event.startswith(("socket.", "os.exec", "os.posix_spawn")) or event in {
        "subprocess.Popen",
        "os.system",
        "os.fork",
    }:
        raise RuntimeError(f"Offline benchmark attempted {event}")


def issue_rows(size: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    issues = []
    pulls = []
    for i in range(size):
        number = 1000 + i
        body = f"## Outcome\nImprove the café backlog report for cohort {i % 23}.\n"
        body += "\n".join(
            f"- Verify that fixture {row} preserves the expected status."
            for row in range(2 + i % 19)
        )
        if i > 1 and i % 3 == 0:
            body += f"\n\n## Prerequisites\n- **#{number - 1}**\n- `#{number - 2}`\n"
        elif i % 3 == 1:
            body += "\n\nDepends on #900 and [#901](https://example.invalid/901), see #999.\n"
        if i % 11 == 0:
            body += "\n\n```markdown\nmanual-only\n```\n"
        if i % 17 == 0:
            body += "\nmanual-only\n"
        issues.append(
            {
                "number": number,
                "state": "OPEN",
                "title": f"Report item {i}",
                "body": body,
                "labels": [{"name": "factory:unowned"}],
            }
        )
        if i % 8 == 0:
            pulls.append(
                {
                    "number": 5000 + i,
                    "body": body + f"\nPart of #999\nDoes not fix #998\nCloses #{number}.",
                    "state": "OPEN",
                    "headRefName": f"implementation-{number}",
                    "labels": [{"name": "factory:review"}],
                }
            )
    return issues, pulls


class FixtureGitHub:
    def __init__(self, checks: int) -> None:
        self.head = "a" * 40
        self.jobs = []
        self.logs = {}
        errors = (
            "Diff in /home/runner/work/example/example/src/lib.rs:12:",
            "src/组件.ts(4,2): error TS1234: invalid types",
            "FAILED tests/test_fixture.py::test_edge",
            "test part_a ... FAILED",
            "\x1b[31merror[E0204]: échec de compilation\x1b[0m",
            "::error::unexpected job failure",
            "##[error]Process completed with exit code 1.",
        )
        for i in range(checks):
            self.jobs.append(
                {"id": i, "name": f"check-{i}", "conclusion": "failure", "html_url": f"/job/{i}"}
            )
            repeats = 16 * (1 + i % 32)
            filler = "".join(
                f"2026-11-01T01:02:03Z Checking fixture step {x}\n" for x in range(repeats)
            )
            self.logs[f"actions/jobs/{i}/logs"] = filler + errors[i % len(errors)]

    def repo_api(self, endpoint: str) -> dict[str, Any]:
        if endpoint == "pulls/7":
            return {"head": {"sha": self.head}}
        if endpoint == f"commits/{self.head}/check-runs?per_page=100":
            return {"check_runs": self.jobs}
        raise AssertionError(endpoint)

    def text(self, endpoint: str, *, final: str) -> str:
        assert final == "completed"
        return self.logs[endpoint]


def run(source: Path, size: int, repeats: int, selection: str) -> dict[str, Any]:
    sys.path[:0] = [str(source / "src"), str(source / "factory/.github/scripts")]
    sys.addaudithook(offline_guard)
    os.environ["GITHUB_REPOSITORY"] = "fixture/native-benchmark"
    policy = importlib.import_module("factory_work_policy")
    deps = importlib.import_module("factory_deps")
    checks = importlib.import_module("rotisserie.factory_console.service.checks")
    estimate = importlib.import_module("rotisserie.factory_console.service.estimate_model")
    issues, pulls = issue_rows(size)
    gh = FixtureGitHub(max(7, size // 8))
    windows = [[float((j * j + i * j) % 99991) for j in range(8 + i % 200)] for i in range(size)]

    def scheduling() -> list[dict[str, Any]]:
        return [asdict(c) for c in policy.build_candidates(issues, pulls, incidents={})]

    def dependencies() -> list[Any]:
        graph = deps.graph(issues)
        return [graph.cycles(), [graph.blockers(i["number"]) for i in issues]]

    def check_logs() -> list[dict[str, Any]]:
        return [c.to_dict() for c in checks.failing_checks(gh, 7)]

    def estimates() -> list[dict[str, object] | None]:
        return [estimate.spread(window) for window in windows]

    workloads = {
        "scheduling": scheduling,
        "dependencies": dependencies,
        "check-logs": check_logs,
        "estimates": estimates,
    }
    results = {}
    for name, workload in workloads.items():
        if selection not in ("all", name):
            continue
        payload = workload()
        assert payload, name
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=sorted).encode()
        ).hexdigest()
        measurements = []
        for _ in range(repeats):
            start = time.process_time_ns()
            workload()
            measurements.append((time.process_time_ns() - start) / 1e6)
        results[name] = {
            "result_sha256": digest,
            "cpu_median_ms": statistics.median(measurements),
            "cpu_samples_ms": measurements,
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--size", type=int, default=384)
    parser.add_argument("--runs", type=int, default=11)
    parser.add_argument(
        "--workload",
        choices=["all", "scheduling", "dependencies", "check-logs", "estimates"],
        default="all",
    )
    args = parser.parse_args()
    print(json.dumps(run(args.source.resolve(), args.size, args.runs, args.workload), indent=2))


if __name__ == "__main__":
    main()
