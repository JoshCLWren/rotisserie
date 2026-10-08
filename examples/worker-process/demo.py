"""Run a complete temporary local worker handoff without a hosting provider."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from rotisserie.application.process import assignment_to_dict
from rotisserie.application.runtime import Assignment, AssignmentId, Authority, AuthorityManifest
from rotisserie.cli import main
from rotisserie.domain import (
    GraphSnapshot,
    Lease,
    LeaseId,
    RepositoryId,
    Work,
    Worker,
    WorkerId,
    WorkId,
)


def demo() -> int:
    with tempfile.TemporaryDirectory(prefix="rotisserie-worker-demo-") as directory:
        root = Path(directory)
        worktree = root / "worktree"
        subprocess.run(["git", "init", "--quiet", str(worktree)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "remote",
                "add",
                "origin",
                "https://example.test/rotisserie/demo.git",
            ],
            check=True,
        )
        repository = RepositoryId("example.test", "rotisserie", "demo")
        work = WorkId(repository, "1")
        worker = WorkerId("demo", "one")
        at = int(time.time())
        graph = GraphSnapshot(
            works=(Work(work, "Demonstrate a worker handoff"),),
            workers=(Worker(worker),),
            leases=(Lease(LeaseId("demo"), work, worker, at, at + 60),),
        )
        (root / "graph.json").write_text(json.dumps(graph.to_dict()), encoding="utf-8")
        assignment = Assignment(
            AssignmentId("demo"),
            worker,
            "Demonstrate the process contract",
            AuthorityManifest(work, None, frozenset({Authority.READ}), at + 60),
            "Example repository content",
            timeout_seconds=10,
        )
        assignment_path = root / "assignment.json"
        assignment_path.write_text(json.dumps(assignment_to_dict(assignment)), encoding="utf-8")
        bridge = Path(__file__).with_name("bridge.py").resolve()
        config = root / "config.toml"
        config.write_text(
            'schema_version = 1\n[repository]\nhost = "example.test"\n'
            'owner = "rotisserie"\nname = "demo"\n'
            '[repositories]\nallow = [{host="example.test", owner="rotisserie", name="demo"}]\n'
            '[local]\nsnapshot = "graph.json"\nstate_directory = "state"\n'
            '[workers]\nexpected = ["demo:one"]\n'
            '[execution]\nenabled = true\nworktree = "worktree"\n'
            '[[execution.executors]]\nname = "example"\n'
            f"argv = {json.dumps([sys.executable, str(bridge)])}\n",
            encoding="utf-8",
        )
        args = ["--config", str(config), "execute", "--assignment", str(assignment_path)]
        for suffix in ([], ["--apply"], ["--apply"]):
            code = main([*args, *suffix])
            if code:
                return code
        return 0


if __name__ == "__main__":
    raise SystemExit(demo())
