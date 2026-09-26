"""Safety boundary for GitHub Actions enabled in Rotisserie."""

from pathlib import Path


WORKFLOWS = Path(".github/workflows")
ALLOWED_WORKFLOWS = {"ci.yml"}
FORBIDDEN_TEXT = (
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


def test_only_rotisserie_ci_is_active() -> None:
    """Keep imported ComicPile automation outside the executable directory."""
    active = {path.name for path in WORKFLOWS.iterdir() if path.is_file()}
    assert active == ALLOWED_WORKFLOWS


def test_active_actions_are_read_only_and_rotisserie_specific() -> None:
    """Reject schedules, write permissions, and ComicPile coupling."""
    for path in WORKFLOWS.iterdir():
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8").lower()
        for forbidden in FORBIDDEN_TEXT:
            assert forbidden not in source, f"{path} contains forbidden text: {forbidden}"
