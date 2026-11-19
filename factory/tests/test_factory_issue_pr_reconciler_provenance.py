"""Regression checks for the trusted issue/PR reconciliation verifier."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/factory-issue-pr-state-reconciler.yml"
BRANCH = "factory/39-1477-opencode-free"
REPO = "JoshCLWren/comic-pile"
HEAD = "a" * 40
MARKER = (
    "<!-- comic-pile-factory-pr-provenance-v1:"
    f"pr-1517:issue-1477:opencode-free-model-factory-39:1787060686:{HEAD} -->"
)


def _verifier_script() -> str:
    """Extract the actual inline verifier, avoiding a copied test implementation."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"python3 - \"\$head_ref\".*?<<'PY'\n(.*?)\n          PY", workflow, re.DOTALL)
    assert match is not None
    return textwrap.dedent(match.group(1))


def _reconcile_shell() -> str:
    """Extract the real reconciliation shell script from the workflow run step."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    # The workflow also has a quota-guard run step. Select the actual issue
    # reconciler step, not the first shell block in the YAML.
    step = workflow.index("      - name: Reconcile linked issue from trusted canonical PR history")
    marker = "          set -Eeuo pipefail\n"
    return textwrap.dedent(workflow[workflow.index(marker, step) :])


def _stub(bin_dir: Path, name: str, body: str) -> None:
    """Install a PATH-resolved stub with the given bash body."""
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _run_reconciler(bin_dir: Path, *, event: str) -> subprocess.CompletedProcess[str]:
    """Run the real workflow shell with a stubbed gh and no-op sleep."""
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": REPO,
        "EVENT_NAME": event,
        "EVENT_ACTION": "opened" if event == "pull_request_target" else "",
        "EVENT_PR_NUMBER": "1517" if event == "pull_request_target" else "",
        "EVENT_HEAD_REF": BRANCH if event == "pull_request_target" else "",
        "EVENT_HEAD_REPO": REPO if event == "pull_request_target" else "",
    }
    return subprocess.run(
        ["bash", "-c", _reconcile_shell()],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("comments", "expected"),
    [
        ([{"body": MARKER, "user": {"login": "github-actions[bot]"}}], 0),
        ([{"body": "untrusted", "user": {"login": "github-actions[bot]"}}], 1),
        ([{"body": MARKER, "user": {"login": "untrusted-bot"}, "author_association": "NONE"}], 1),
    ],
)
def test_reconciler_provenance_outcomes(
    tmp_path: Path, comments: list[dict[str, object]], expected: int
) -> None:
    """The real verifier accepts only trusted matching provenance."""
    comment_file = tmp_path / "comments.json"
    comment_file.write_text(json.dumps(comments), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-", BRANCH, "1517", "1477", str(comment_file)],
        input=_verifier_script(),
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == expected, result.stderr


def test_reconciler_verifier_error_is_not_missing_provenance(tmp_path: Path) -> None:
    """Operational failures must return 2, never the retryable missing-marker 1."""
    comment_file = tmp_path / "invalid.json"
    comment_file.write_text("{invalid json", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-", BRANCH, "1517", "1477", str(comment_file)],
        input=_verifier_script(),
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 2
    assert "JSONDecodeError" in result.stderr


def test_comment_api_failure_is_not_a_green_run(tmp_path: Path) -> None:
    """A gh API failure on the provenance fetch must fail, never exit green."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "sleep", "exit 0\n")
    _stub(
        bin_dir,
        "gh",
        f"""if [[ "$*" == *"/pulls/1517"* ]]; then
  printf '%s' '{{"head":{{"ref":"{BRANCH}","repo":{{"full_name":"{REPO}"}}}}}}'
  exit 0
fi
if [[ "$*" == *"/issues/1517/comments"* ]]; then
  echo "gh: HTTP 502 from api.github.com" >&2
  exit 1
fi
echo "unexpected gh invocation: $*" >&2
exit 1
""",
    )

    result = _run_reconciler(bin_dir, event="pull_request_target")

    assert result.returncode == 2, result.stderr
    assert "HTTP 502" in result.stderr


def test_sweep_issue_list_failure_is_not_a_green_run(tmp_path: Path) -> None:
    """A failed scheduled issue list must fail instead of skipping the sweep."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "sleep", "exit 0\n")
    _stub(
        bin_dir,
        "gh",
        """if [[ "$*" == *"/pulls?state=all"* ]]; then
  printf '%s' '[]'
  exit 0
fi
if [[ "$1 $2" == "issue list" ]]; then
  echo "gh: rate limit exceeded" >&2
  exit 1
fi
echo "unexpected gh invocation: $*" >&2
exit 1
""",
    )

    result = _run_reconciler(bin_dir, event="schedule")

    assert result.returncode != 0, result.stderr
    assert "rate limit exceeded" in result.stderr
