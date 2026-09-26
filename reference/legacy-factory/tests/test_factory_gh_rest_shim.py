"""Regression coverage for the factory GitHub REST compatibility shim."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM = REPO_ROOT / ".github/scripts/factory-gh-rest-shim.sh"


def _fake_gh(tmp_path: Path) -> Path:
    fake = tmp_path / "gh-real"
    fake.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$1" == api ]] || { echo "unexpected command: $*" >&2; exit 9; }
shift
[[ "${1:-}" == --paginate ]] && shift || true
[[ "${1:-}" == --slurp ]] && shift || true
endpoint="$1"
case "$endpoint" in
  *'/issues?'*)
    printf '%s\\n' '[[{"number":10,"title":"Issue","body":"body","state":"open","created_at":"2026-08-16T00:00:00Z","updated_at":"2026-08-16T01:00:00Z","labels":[{"name":"factory:13"},{"name":"bug"}]},{"number":11,"title":"PR row","body":"body","state":"open","pull_request":{},"created_at":"2026-08-16T00:00:00Z","updated_at":"2026-08-16T01:00:00Z","labels":[{"name":"factory:13"}]}]]'
    ;;
  *'/pulls?'*)
    printf '%s\\n' '[[{"number":20,"title":"PR","body":"pbody","state":"open","draft":false,"created_at":"2026-08-16T00:00:00Z","updated_at":"2026-08-16T01:00:00Z","labels":[{"name":"factory:13"}],"head":{"ref":"factory/13-10-x","sha":"abc"}}]]'
    ;;
  *'/pulls/20')
    printf '%s\\n' '{"number":20,"title":"PR","body":"pbody","state":"open","merged_at":null,"mergeable":true,"draft":false,"labels":[{"name":"factory:13"}],"head":{"ref":"factory/13-10-x","sha":"abc"}}'
    ;;
  *'/issues/10')
    printf '%s\\n' '{"number":10,"title":"Issue","body":"body","state":"open","labels":[{"name":"factory:13"}]}'
    ;;
  *) echo "unknown endpoint: $endpoint" >&2; exit 8 ;;
esac
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "FACTORY_REAL_GH": str(_fake_gh(tmp_path)),
            "GITHUB_REPOSITORY": "JoshCLWren/comic-pile",
        }
    )
    return subprocess.run(
        ["bash", str(SHIM), *args],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )


def _fake_checks_gh(
    tmp_path: Path,
    *,
    checks_message: str,
    checks_status: int,
    protected: bool,
) -> Path:
    """Create a gh stand-in for required-check normalization tests."""
    fake = tmp_path / "gh-checks-real"
    protected_json = "true" if protected else "false"
    checks_shell = shlex.quote(checks_message)
    fake.write_text(
        f"""#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$1" == pr && "$2" == checks ]]; then
  printf '%s\\n' {checks_shell} >&2
  exit {checks_status}
fi
if [[ "$1" == api && "$2" == repos/JoshCLWren/comic-pile/pulls/1390 ]]; then
  printf '%s\\n' '{{"base":{{"ref":"main"}}}}'
  exit 0
fi
if [[ "$1" == api && "$2" == repos/JoshCLWren/comic-pile/branches/main ]]; then
  printf '%s\\n' '{{"protected":{protected_json}}}'
  exit 0
fi
printf 'unexpected command: %s\\n' "$*" >&2
exit 97
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_checks(fake_gh: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "FACTORY_REAL_GH": str(fake_gh),
            "GITHUB_REPOSITORY": "JoshCLWren/comic-pile",
        }
    )
    return subprocess.run(
        [
            "bash",
            str(SHIM),
            "pr",
            "checks",
            "1390",
            "--repo",
            "JoshCLWren/comic-pile",
            "--required",
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_rest_shim_lists_only_matching_issues(tmp_path: Path) -> None:
    """Issue enumeration avoids GraphQL and excludes pull-request rows."""
    result = _run(
        tmp_path,
        "issue",
        "list",
        "--state",
        "open",
        "--limit",
        "300",
        "--label",
        "factory:13",
        "--json",
        "number",
    )
    assert [item["number"] for item in json.loads(result.stdout)] == [10]


def test_rest_shim_normalizes_pr_list_and_view(tmp_path: Path) -> None:
    """PR enumeration and view expose the fields used by factory wrappers."""
    listed = _run(
        tmp_path,
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "200",
        "--label",
        "factory:13",
        "--json",
        "number,labels",
    )
    assert [item["number"] for item in json.loads(listed.stdout)] == [20]

    viewed = _run(
        tmp_path,
        "pr",
        "view",
        "20",
        "--json",
        "headRefName",
        "--jq",
        ".headRefName",
    )
    assert viewed.stdout.strip() == "factory/13-10-x"


def test_rest_shim_normalizes_issue_view_state(tmp_path: Path) -> None:
    """Issue view preserves the uppercase state contract expected by workers."""
    viewed = _run(
        tmp_path,
        "issue",
        "view",
        "10",
        "--json",
        "state",
        "--jq",
        ".state",
    )
    assert viewed.stdout.strip() == "OPEN"


def test_no_checks_are_satisfied_on_explicitly_unprotected_base(tmp_path: Path) -> None:
    """GitHub CLI's no-check error is not a failed gate when the base is unprotected."""
    fake = _fake_checks_gh(
        tmp_path,
        checks_message="no checks reported on the 'factory/43-1386-opencode-free' branch",
        checks_status=1,
        protected=False,
    )
    result = _run_checks(fake)
    assert result.returncode == 0
    assert "no required checks configured on unprotected base main" in result.stderr


def test_no_required_checks_are_satisfied_on_unprotected_base(tmp_path: Path) -> None:
    """The CLI's alternate no-required-checks message receives the same narrow exception."""
    fake = _fake_checks_gh(
        tmp_path,
        checks_message=(
            "no required checks reported on the 'factory/43-1386-opencode-free' branch"
        ),
        checks_status=1,
        protected=False,
    )
    result = _run_checks(fake)
    assert result.returncode == 0


def test_no_required_checks_still_fail_on_protected_base(tmp_path: Path) -> None:
    """Protected branches remain fail-closed even when GitHub reports no checks."""
    message = "no required checks reported on the 'factory/43-1386-opencode-free' branch"
    fake = _fake_checks_gh(
        tmp_path,
        checks_message=message,
        checks_status=1,
        protected=True,
    )
    result = _run_checks(fake)
    assert result.returncode == 1
    assert message in result.stderr


def test_real_required_check_failure_is_never_normalized(tmp_path: Path) -> None:
    """Only the CLI's specific no-check result may receive the exception."""
    message = "Unit Tests\tfail\t42s"
    fake = _fake_checks_gh(
        tmp_path,
        checks_message=message,
        checks_status=1,
        protected=False,
    )
    result = _run_checks(fake)
    assert result.returncode == 1
    assert message in result.stderr


def test_successful_required_checks_pass_through(tmp_path: Path) -> None:
    """Normal successful check execution remains untouched."""
    message = "Unit Tests\tpass\t38s"
    fake = _fake_checks_gh(
        tmp_path,
        checks_message=message,
        checks_status=0,
        protected=True,
    )
    result = _run_checks(fake)
    assert result.returncode == 0
    assert message in result.stdout


def test_dispatcher_and_worker_install_rest_shim() -> None:
    """Both control-plane assignment and lease consumption use the REST shim."""
    dispatcher = (
        REPO_ROOT / ".github/workflows/fixed-model-factory-dispatch.yml"
    ).read_text(encoding="utf-8")
    worker = (
        REPO_ROOT / ".github/scripts/free-model-factory-worker.sh"
    ).read_text(encoding="utf-8")

    assert "Install REST-backed factory GitHub reads" in dispatcher
    assert "factory-gh-rest-shim.sh" in dispatcher
    assert "Unable to enumerate factory:ready PRs" in dispatcher
    assert "install_factory_rest_gh" in worker
    assert "factory-gh-rest-shim.sh" in worker
