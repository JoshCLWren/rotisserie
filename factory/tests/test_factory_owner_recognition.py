"""Regression coverage for roster-derived factory owner-label recognition.

Roster workers now include IDs above 100, with membership checked against the roster. Lease and review code must recognize all
canonical positive numeric owners while assignment verifies actual membership.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"

EXPECTED_OWNER_RE = r"^factory:(unowned|local|[1-9][0-9]*)$"

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
    """Worker shell scripts must agree with the shared no-ceiling label pattern."""
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
        "factory:100",
        "factory:101",
        "factory:102",
        "factory:103",
        "factory:104",
        "factory:105",
        "factory:106",
        "factory:107",
        "factory:unowned",
        "factory:local",
    ):
        assert pattern.fullmatch(label), f"{label} must be a recognized factory owner"


def test_three_digit_factory_owners_are_recognized_but_malformed_are_rejected() -> None:
    """Recognize valid IDs without granting unknown IDs assignment authority."""
    pattern = re.compile(EXPECTED_OWNER_RE)
    for label in (
        "factory:80", "factory:99", "factory:100", "factory:104",
        "factory:107", "factory:999",
    ):
        assert pattern.fullmatch(label), f"{label} must be a recognizable owner"
    for label in (
        "factory:0", "factory:00", "factory:01", "factory:0104",
        "factory:abc", "factory:",
    ):
        assert not pattern.fullmatch(label), f"{label} must not be a valid owner"


def test_generated_slot_ids_are_authorized_without_widening_to_unknown_workers() -> None:
    """Generated provider slots are authorized; unknown decimal IDs are not.

    The TSV roster was removed (#5213); slot identities are derived from the
    provider integration registry by factory_worker_slots.
    """
    import sys

    sys.path.insert(0, str(SCRIPTS))
    import factory_worker_slots

    roster_sources = {slot.worker: slot.source for slot in factory_worker_slots.slots()}
    roster_ids = set(roster_sources)
    three_digit_workers = sorted(int(worker) for worker in roster_ids)
    assert three_digit_workers

    # Load the controller with the established importlib test pattern and bind
    # its worker_sources() to the parsed roster mapping so authorization is
    # exercised against real current IDs rather than a tautological subset.
    import importlib.util
    import sys

    controller_path = ROOT / ".github" / "scripts" / "factory-work-controller.py"
    spec = importlib.util.spec_from_file_location(
        "factory_work_controller_owner_recognition", controller_path
    )
    assert spec is not None and spec.loader is not None
    controller = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = controller
    spec.loader.exec_module(controller)
    controller.__dict__["worker_sources"] = lambda: roster_sources

    for worker in three_digit_workers:
        assert controller.is_authorized_fixed_worker(str(worker)), worker
    unknown_worker = str(max(map(int, roster_ids)) + 1)
    assert unknown_worker not in roster_ids
    assert not controller.is_authorized_fixed_worker(unknown_worker)
    for malformed in ("0", "00", "01", "0104", "+104", "104 ", "abc", ""):
        assert not controller.is_authorized_fixed_worker(malformed)

    # Fresh free-model workers may legitimately retire; do not freeze their IDs.
    assert all(re.fullmatch(r"[1-9][0-9]*", worker) for worker in roster_ids)

    # The controller checks both canonical decimal spelling and roster membership.
    source = (SCRIPTS / "factory-work-controller.py").read_text(encoding="utf-8")
    assert "worker in worker_sources()" in source
    assert "unsupported fixed-model worker" in source
    assert "is_authorized_fixed_worker(worker)" in source



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
    # Model/provider switching is the trusted wrapper's fleet failover within
    # the same lease (#5280), never agent-initiated route adaptation.
    assert "select_next_eligible_model" in text
    assert "Do not switch models, providers, or routes." in text


def test_factory_persistence_pushes_and_verifies_current_head() -> None:
    """A local worktree branch mismatch must not discard a worker's commit."""
    primitives = (SCRIPTS / "free-model-factory-worker-primitives.sh").read_text(encoding="utf-8")
    # Worker 48 installation-token pushes go through factory_git_push so the
    # token is masked and the origin URL is restored after the push.
    assert "factory_git_push" in primitives
    assert "factory_git_push \"$push_source\" \"$push_token\" --set-upstream origin \"HEAD:$branch\"" in primitives
    assert "factory_git_push \"${push_credential%%$'\\t'*}\" \"${push_credential#*$'\\t'}\" origin \"HEAD:$branch\"" in primitives
    assert 'git ls-remote origin "refs/heads/${branch}"' in primitives
    # Minted installation tokens are not repository secrets; Actions will not
    # mask them on its own.
    assert "printf '::add-mask::%s\\n' \"$token\"" in primitives
    # PR_REBASE_TOKEN / non-installation path still uses plain git push inside
    # factory_git_push (no URL swap) for internal heads. Fork repair swaps
    # the URL for this push only, with the same credential masking/restoration.
    assert 'if [[ "$source" != installation && -z "${FACTORY_PUSH_REPOSITORY:-}" ]]; then' in primitives
    assert 'git push "$@"' in primitives
    assert 'PR_REBASE_TOKEN' in primitives
    for path in (
        SCRIPTS / "omniroute-factory-worker.sh",
        SCRIPTS / "nvidia-factory-worker.sh",
    ):
        text = path.read_text(encoding="utf-8")
        assert 'git push origin "HEAD:$branch"' in text or 'git push --set-upstream origin "HEAD:$branch"' in text
        assert 'git ls-remote origin "refs/heads/${branch}"' in text
