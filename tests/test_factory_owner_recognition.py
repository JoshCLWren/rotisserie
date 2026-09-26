"""Regression coverage for shared factory owner-label recognition boundaries.

Issue #1178 raised the shared NVIDIA and OmniRoute worker owner boundary from
``factory:16`` through ``factory:17``. The fixed-model roster has since grown
(see ``.github/free-model-factories.tsv``, workers 6-71), so every worker must
now use the canonical ``factory_work_policy`` boundary covering
``factory:1``-``factory:79``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"

EXPECTED_OWNER_RE = r"^factory:(unowned|local|[1-9]|[1-3][0-9]|[4-7][0-9])$"

OWNER_RE_SCRIPTS = (
    SCRIPTS / "omniroute-factory-worker.sh",
    SCRIPTS / "nvidia-factory-worker.sh",
)


def _extract_owner_re(path: Path) -> str:
    """Read the OWNER_RE assignment from a bash worker script.

    Args:
        path: The worker script to inspect.

    Returns:
        The OWNER_RE pattern string.
    """
    text = path.read_text(encoding="utf-8")
    match = re.search(r"OWNER_RE='([^']+)'", text)
    assert match is not None, f"{path.name} must define OWNER_RE"
    return match.group(1)


def test_shared_owner_re_matches_canonical_boundary() -> None:
    """Every shared worker must use the canonical 1-79 owner boundary."""
    for path in OWNER_RE_SCRIPTS:
        owner_re = _extract_owner_re(path)
        assert owner_re == EXPECTED_OWNER_RE, (
            f"{path.name} must match the canonical factory_work_policy boundary, got {owner_re!r}"
        )


def test_roster_workers_are_recognized_as_owned() -> None:
    """Every roster worker label must be treated as owned by the shared workers."""
    pattern = re.compile(EXPECTED_OWNER_RE)
    for label in (
        "factory:1",
        "factory:9",
        "factory:17",
        "factory:39",
        "factory:46",
        "factory:60",
        "factory:71",
        "factory:79",
        "factory:unowned",
        "factory:local",
    ):
        assert pattern.fullmatch(label), f"{label} must be a recognized factory owner"


def test_factory_80_and_above_remain_out_of_scope() -> None:
    """The recognition boundary stops at factory:79, not factory:80+."""
    pattern = re.compile(EXPECTED_OWNER_RE)
    for label in ("factory:80", "factory:99", "factory:100", "factory:abc", "factory:"):
        assert not pattern.fullmatch(label), (
            f"{label} must remain outside the shared owner boundary"
        )


def test_nvidia_worker_keeps_provider_and_model_behavior() -> None:
    """The NVIDIA worker must retain its positional model contract and probe."""
    text = (SCRIPTS / "nvidia-factory-worker.sh").read_text(encoding="utf-8")
    assert 'MODEL="${2:?model required}"' in text, (
        "NVIDIA worker must keep its positional MODEL argument"
    )
    assert "FCM_NVIDIA_MODEL_OK" in text, (
        "NVIDIA worker must keep its NVIDIA model probe"
    )
    assert "OMNIROUTE_MODEL" not in text, (
        "NVIDIA worker must not adopt OmniRoute model handling"
    )


def test_omniroute_worker_keeps_runtime_model_contract() -> None:
    """The OmniRoute worker must retain its environment model contract."""
    text = (SCRIPTS / "omniroute-factory-worker.sh").read_text(encoding="utf-8")
    assert 'MODEL="${OMNIROUTE_MODEL:?OMNIROUTE_MODEL is required}"' in text


def test_omniroute_worker_allows_gateway_route_adaptation() -> None:
    """OmniRoute may change upstream capacity without a prompt-level veto."""
    text = (SCRIPTS / "omniroute-factory-worker.sh").read_text(encoding="utf-8")
    assert "OmniRoute may switch upstream models, providers, or routes" in text


def test_shared_factory_wrapper_rejects_omniroute_while_incident_dark() -> None:
    """Incident restore keeps OmniRoute dark and refuses omniroute-free Entry."""
    text = (SCRIPTS / "free-model-factory-worker.sh").read_text(encoding="utf-8")
    assert "OmniRoute Entry is disabled for this incident" in text
    assert "FACTORY_SOURCE}" in text
    assert "== 'omniroute-free'" in text
    assert "GitHub factory execution is OmniRoute-only" not in text
    # Multi-provider Entry pins the leased model; do not revive OmniRoute route adaptation.
    assert "refusing to switch models" in text


def test_factory_persistence_pushes_and_verifies_current_head() -> None:
    """A local worktree branch mismatch must not discard a worker's commit."""
    for path in (
        SCRIPTS / "free-model-factory-worker-primitives.sh",
        SCRIPTS / "omniroute-factory-worker.sh",
        SCRIPTS / "nvidia-factory-worker.sh",
    ):
        text = path.read_text(encoding="utf-8")
        assert 'git push origin "HEAD:$branch"' in text or 'git push --set-upstream origin "HEAD:$branch"' in text
        assert 'git ls-remote origin "refs/heads/${branch}"' in text
