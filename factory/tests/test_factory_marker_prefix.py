"""Comment markers derive their prefix from the adopter repository name.

Markers already posted on an adopter's issues and pull requests must keep
matching, so a repository named comic-pile must still produce and recognize
the exact strings it wrote before the prefix was derived.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FACTORY = Path(__file__).resolve().parents[1]
SCRIPTS = FACTORY / ".github" / "scripts"
HEAD = "a" * 40

PYTHON_PROBE = f"""
import importlib.util, json, sys
sys.path.insert(0, {str(SCRIPTS)!r})

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, {str(SCRIPTS)!r} + "/" + filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

import factory_adopter, factory_review_policy as review, factory_post_merge_closure_policy as closure
import factory_deploy_recovery as deploy, factory_no_progress_retirement as retire
controller = load("factory_work_controller", "factory-work-controller.py")
claim = "<!-- comic-pile-factory-controller-claim-v1:issue-12:48:1700000000 -->"
print(json.dumps({{
    "prefix": factory_adopter.marker_prefix(),
    "review": review.review_marker(pr=44, head="{HEAD}", reviewer="7", producer="8", verdict="approve"),
    "contributor": review.head_contributor_marker(pr=7, head="{HEAD}", worker="12", epoch=1700000000),
    "author": review.agent_model_author_marker(pr=88, head="{HEAD}", model="m", agent="a"),
    "closure": closure.MERGE_CLOSURE_MARKER,
    "deploy": deploy.MARKER,
    "retire": retire.RETIRE_MARKER,
    "claim_matches": any(p.search(claim) for p in controller.LEASE_ACTIVITY_PATTERNS),
}}))
"""


def _env(repository: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "FACTORY_MARKER_PREFIX"}
    env["GITHUB_REPOSITORY"] = repository
    return env


def _python(repository: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", PYTHON_PROBE],
        env=_env(repository),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def _shell(repository: str) -> str:
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{SCRIPTS}/factory-review-findings.sh"; printf %s "$FACTORY_MARKER_PREFIX"',
        ],
        env=_env(repository),
        cwd=FACTORY,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout


def _node(repository: str) -> str:
    result = subprocess.run(
        [
            "node",
            "-e",
            f"process.stdout.write(require({str(SCRIPTS / 'factory-visibility.cjs')!r})._test.markerPrefix())",
        ],
        env=_env(repository),
        cwd=FACTORY,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout


def test_comic_pile_repository_yields_its_existing_marker_strings() -> None:
    markers = _python("JoshCLWren/comic-pile")
    assert markers == {
        "prefix": "comic-pile",
        "review": f"<!-- comic-pile-factory-semantic-review-v1:pr-44:head-{HEAD}:reviewer-7:producer-8:verdict-approve -->",
        "contributor": f"<!-- comic-pile-factory-head-contributor-v1:pr-7:head-{HEAD}:worker-12:epoch-1700000000 -->",
        "author": f"<!-- comic-pile-agent-model-author-v1:pr-88:head-{HEAD}:model-m:agent-a -->",
        "closure": "comic-pile-factory-merge-closure-v1",
        "deploy": "<!-- comic-pile-deploy-recovery:v1 -->",
        "retire": "comic-pile-factory-no-progress-retired-v1",
        "claim_matches": True,
    }


def test_another_repository_gets_its_own_prefix_and_ignores_comic_pile_markers() -> None:
    markers = _python("acme/widgets")
    assert markers["prefix"] == "widgets"
    assert markers["closure"] == "widgets-factory-merge-closure-v1"
    assert markers["deploy"] == "<!-- widgets-deploy-recovery:v1 -->"
    assert markers["claim_matches"] is False


@pytest.mark.parametrize(
    ("repository", "prefix"),
    [
        ("JoshCLWren/comic-pile", "comic-pile"),
        ("acme/widgets", "widgets"),
        ("Acme/My.Repo_2", "my-repo-2"),
    ],
)
def test_python_shell_and_node_derive_the_same_prefix(repository: str, prefix: str) -> None:
    assert _python(repository)["prefix"] == prefix
    assert _shell(repository) == prefix
    assert _node(repository) == prefix
