"""Safety boundary for GitHub Actions enabled in Rotisserie."""

import re
from pathlib import Path

WORKFLOWS = Path(".github/workflows")
ALLOWED_WORKFLOWS = {"ci.yml", "canary-merge.yml"}
FORBIDDEN_TEXT: tuple[str, ...] = (
    "comic-pile",
    "comic_pile",
    "joshclwren/comic-pile",
    "schedule:",
    "contents: write",
    "issues: write",
    "pull-requests: write",
    "actions: write",
    "pages: write",
    "id-token: write",
)


def test_only_approved_rotisserie_workflows_are_active() -> None:
    """Keep imported legacy automation outside the executable directory."""
    active = {path.name for path in WORKFLOWS.iterdir() if path.is_file()}
    assert active == ALLOWED_WORKFLOWS


def test_active_actions_have_no_schedule_or_legacy_repository_coupling() -> None:
    """Reject schedules and legacy repository coupling."""
    for path in WORKFLOWS.iterdir():
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8").lower()
        forbidden_text = FORBIDDEN_TEXT
        if path.name == "canary-merge.yml":
            forbidden_text = tuple(item for item in FORBIDDEN_TEXT if not item.endswith(": write"))
        for forbidden in forbidden_text:
            assert forbidden not in source, f"{path} contains forbidden text: {forbidden}"


def test_canary_workflow_is_manual_narrow_and_kill_switched() -> None:
    source = (WORKFLOWS / "canary-merge.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in source
    assert "\n  pull_request:\n" not in source
    assert "\n  push:\n" not in source
    assert "ROTISSERIE_CANARY_ENABLED" in source
    assert "github.ref == 'refs/heads/main'" in source
    assert "persist-credentials: false" in source
    assert "contents: write" in source
    assert "pull-requests: write" in source
    assert "issues: write" not in source
    permissions = re.search(r"(?m)^permissions:\n((?:  [^\n]+\n)+)", source)
    assert permissions is not None
    assert {line.strip() for line in permissions.group(1).splitlines()} == {
        "contents: write",
        "pull-requests: write",
    }
    run = source.split("run: >-", 1)[1]
    assert "${{ inputs." not in run
    assert '"$CANARY_PULL_REQUEST"' in run
    assert '"$CANARY_EXPECTED_HEAD"' in run
    assert source.count("GH_TOKEN:") == 1
    assert re.search(r"(?m)^        env:\n(?:          [^\n]+\n)*          GH_TOKEN:", source)
