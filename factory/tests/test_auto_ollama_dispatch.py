"""Automatic local Ollama review remains part of the canonical factory cadence."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / ".github/workflows/fixed-model-factory-dispatch.yml"
REVIEWER = ROOT / ".github/workflows/local-ollama-review.yml"


def dispatch_script() -> str:
    """Extract the executable script from the dedicated roster-dispatch job."""
    source = DISPATCHER.read_text(encoding="utf-8")
    step = source.split(
        "      - name: Offer one unclaimed PR to local Ollama reviewer\n", 1,
    )[1].split("\n      - name:", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


def test_canonical_roster_offers_ollama_without_manual_dispatch() -> None:
    """Keep the automatic Qwen review attached to roster and watchdog ticks."""
    workflow = DISPATCHER.read_text(encoding="utf-8")
    reviewer = REVIEWER.read_text(encoding="utf-8")
    job = workflow.split(
        "      - name: Offer one unclaimed PR to local Ollama reviewer", 1,
    )[1].split("      - name: Queue roster successor outside assignment lease lock", 1)[0]
    assert "github.event_name == 'schedule'" in job
    assert "inputs.mode == 'roster'" in job
    assert "!inputs.target" in job
    assert "factory:review" in job
    assert "factory:unowned" in job
    assert "local-ollama-review.yml" in job
    assert 'gh workflow run local-ollama-review.yml' in job
    assert "github.token" in job
    assert "secrets.REVIEW_PROVIDER_URL || secrets.LOCAL_OLLAMA_URL" in job
    assert "group: local-ollama-review\n" in reviewer
    assert "queue: max" in reviewer
    assert "cancel-in-progress: false" in reviewer


MOCK_GH = '''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args[:2] == ["run", "list"]:
    print(json.dumps([{"status": "in_progress"}] if os.environ.get("BUSY") else []))
elif args[:2] == ["api", "repos/example/comic-pile/issues?state=open&labels=factory%3A77&per_page=1"]:
    print(json.dumps([{"number": 99}] if os.environ.get("LEASED") else []))
elif args[:2] == ["pr", "list"]:
    print(json.dumps([
        {"number": 10, "isDraft": False, "labels": [{"name": "factory:review"}, {"name": "factory:unowned"}],
         "headRefName": "misc/unlinked", "body": "Part of #4737 / #4335."},
        {"number": 11, "isDraft": False, "labels": [{"name": "factory:77"}, {"name": "factory:review"}],
         "headRefName": "fix/issue-11", "body": "Closes #11."},
        {"number": 12, "isDraft": True, "labels": [{"name": "factory:review"}, {"name": "factory:unowned"}],
         "headRefName": "fix/issue-12", "body": "Closes #12."},
        {"number": 13, "isDraft": False, "labels": [{"name": "factory:changes-requested"}, {"name": "factory:unowned"}],
         "headRefName": "fix/issue-13", "body": "Closes #13."},
        {"number": 14, "isDraft": False, "labels": [{"name": "factory:review"}, {"name": "factory:unowned"}],
         "headRefName": "fix/issue-14", "body": "Closes #14."},
    ]))
elif args[:2] == ["api", "repos/example/comic-pile/pulls/14"]:
    print(json.dumps({
        "state": "open", "draft": False, "base": {"ref": "main"},
        "labels": [
            {"name": "factory:unowned"},
            {"name": "factory:changes-requested" if os.environ.get("MOVED") else "factory:review"},
        ],
    }))
elif args[:2] == ["workflow", "run"]:
    with open(os.environ["DISPATCH_LOG"], "a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\\n")
else:
    print(f"unexpected GitHub call: {args}", file=sys.stderr)
    sys.exit(1)
'''


@pytest.mark.parametrize(
    ("extra", "should_dispatch"),
    [
        ({}, True),
        ({"BUSY": "1"}, False),
        ({"LEASED": "1"}, False),
        ({"MOVED": "1"}, False),
    ],
)
def test_auto_dispatch_respects_busy_gpu_factory_leases_and_fresh_pr_stage(
    tmp_path: Path, extra: dict[str, str], should_dispatch: bool,
) -> None:
    """Exercise the actual production Bash selector using deterministic gh mocks."""
    if not shutil.which("jq"):
        pytest.skip("jq is not installed in this environment")
    mock = tmp_path / "gh"
    mock.write_text(MOCK_GH, encoding="utf-8")
    mock.chmod(0o755)
    log = tmp_path / "dispatch.log"
    env = os.environ.copy()
    env.update({
        "PATH": f"{tmp_path}:{env['PATH']}",
        "GITHUB_REPOSITORY": "example/comic-pile",
        "REVIEW_PROVIDER_URL": "https://fake-ollama.invalid",
        "DISPATCH_LOG": str(log),
        **extra,
    })
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", dispatch_script()],
        capture_output=True, text=True, env=env, check=False,
    )
    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8") if log.exists() else ""
    if should_dispatch:
        assert "local-ollama-review.yml" in commands
        assert "-f pr=14" in commands
    else:
        assert not commands


def test_missing_provider_url_leaves_other_workers_undisturbed(tmp_path: Path) -> None:
    """Do not dispatch a broken local-review workflow without provider secrets."""
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", dispatch_script()],
        capture_output=True, text=True, check=False,
        env={"PATH": os.environ["PATH"], "REVIEW_PROVIDER_URL": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "not configured" in result.stdout

def test_ollama_dispatcher_preflights_canonical_issue_links() -> None:
    """An unlinked older PR cannot indefinitely starve a valid review target."""
    script = dispatch_script()
    assert "--json number,isDraft,labels,headRefName,body" in script
    assert "local_review_contract.py select /dev/stdin" in script
    assert 'gh workflow run local-ollama-review.yml' in script
