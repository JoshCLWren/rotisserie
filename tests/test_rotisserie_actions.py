"""Safety boundary for GitHub Actions enabled in Rotisserie."""

import re
from pathlib import Path

from rotisserie.adapters.github.canary import CanaryScope

WORKFLOWS = Path(".github/workflows")
ALLOWED_WORKFLOWS = {"ci.yml", "canary-merge.yml"}
FORBIDDEN_TEXT: tuple[str, ...] = (
    "comic-pile",
    "comic_pile",
    "acme/widgets",
    "schedule:",
    "contents: write",
    "issues: write",
    "pull-requests: write",
    "actions: write",
    "pages: write",
    "id-token: write",
)


def _triggers(source: str) -> set[str]:
    block = re.search(r"(?m)^on:\n((?:(?:  .*)?\n)+)", source)
    assert block is not None
    return set(re.findall(r"(?m)^  ([a-z_]+):", block.group(1)))


def _reusable() -> list[Path]:
    return [
        path
        for path in sorted(WORKFLOWS.iterdir())
        if path.is_file() and path.name not in ALLOWED_WORKFLOWS
    ]


def test_only_approved_rotisserie_workflows_are_active() -> None:
    """Every other workflow is reusable: adopters call it, Rotisserie never triggers it."""
    for path in WORKFLOWS.iterdir():
        assert path.suffix == ".yml", path
    reusable = _reusable()
    assert len(reusable) > 30
    for path in reusable:
        assert _triggers(path.read_text(encoding="utf-8")) == {"workflow_call"}, path.name


def test_reusable_factory_workflows_stay_generic() -> None:
    """Adopter-specific values come from callers, variables or config, never source."""
    for path in _reusable():
        source = path.read_text(encoding="utf-8").lower()
        for forbidden in ("comic-pile", "comic_pile", "comicpile", "joshclwren/comic-pile"):
            assert forbidden not in source, f"{path} contains {forbidden}"
        assert "\nconcurrency:" not in source or path.name == "free-model-factory-run.yml", path
        assert "\nrun-name:" not in source, path


def test_active_actions_have_no_schedule_or_legacy_repository_coupling() -> None:
    """Reject schedules and legacy repository coupling."""
    for path in WORKFLOWS.iterdir():
        if not path.is_file() or path.name not in ALLOWED_WORKFLOWS:
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
    assert source.count("permissions:") == 1
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


def test_canary_required_checks_match_ci_matrix() -> None:
    workflow = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    versions = set(re.findall(r'"(3\.\d+)"', workflow))
    assert CanaryScope("repository", 9, "branch").required_checks == {
        f"Python {version}" for version in versions
    }
