"""Regression coverage for repair-no-change factory stage handoffs."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / ".github" / "scripts" / "free-model-factory-worker-primitives.sh"


def release_stage(
    reason: str,
    fallback_stage: str,
    *,
    current_stage: str = "factory:ready",
    no_diff_limit: int = 1,
    existing_no_diff_comments: int = 5,
) -> str:
    """Run the release primitive and capture the stage passed to replace_labels.

    Git and gh are stubbed so PR no-diff releases can pin a head generation
    without a real checkout or GitHub API. The unused comment-count argument
    stays for call-site compatibility.
    """
    del existing_no_diff_comments
    script = f"""
source <(sed '/^ensure_owner_label$/,$d' \"{PRIMITIVES}\")
current_stage() {{ printf '%s\\n' '{current_stage}'; }}
replace_labels() {{ printf '%s\\n' \"$3\"; }}
git() {{ printf '%s\\n' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; }}
gh() {{
  if [[ \"$1\" == pr && \"$2\" == view ]]; then
    printf '%s\\n' '{{"headRefOid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}}'
    return 0
  fi
  return 0
}}
log() {{ :; }}
release_target 2010 \"{fallback_stage}\" \"{reason}\" pr
"""
    env = os.environ.copy()
    env.update(
        {
            "FACTORY_WORKER": "45",
            "FACTORY_SOURCE": "opencode-free",
            "FACTORY_MODEL": "test-model",
            "FACTORY_RUNTIME_MODEL": "test-runtime",
            "FACTORY_NO_DIFF_RETRY_LIMIT": str(no_diff_limit),
        }
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "release_target failed "
            f"(exit {completed.returncode}): stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    return completed.stdout.strip()


def test_repair_no_change_ready_handoff_cannot_preserve_false_ready() -> None:
    """A no-change READY token still requires independent semantic review."""
    assert release_stage("repair-no-change-ready-handoff", "factory:review") == "factory:review"


def test_repair_no_persisted_change_handoff_preserves_assigned_repair_stage() -> None:
    """A no-change repair without READY cannot inherit an accidental ready label."""
    assert (
        release_stage("repair-no-persisted-change-handoff", "factory:changes-requested")
        == "factory:changes-requested"
    )


def test_other_release_handoffs_still_preserve_current_stage() -> None:
    """The repair-specific guard does not change ordinary lease-release semantics."""
    assert release_stage("repairs-pushed-handoff", "factory:review") == "factory:ready"


def test_repair_no_diff_retry_exhaustion_keeps_changes_requested_not_blocked() -> None:
    """Retry exhaustion must not rewrite a truthful repair stage to factory:blocked."""
    assert (
        release_stage(
            "repair-no-persisted-change-handoff",
            "factory:changes-requested",
            current_stage="factory:changes-requested",
        )
        == "factory:changes-requested"
    )


def test_review_no_diff_retry_exhaustion_keeps_review_not_blocked() -> None:
    """A review/head-change no-diff handoff keeps factory:review plus unowned."""
    assert (
        release_stage(
            "repair-no-persisted-change-handoff",
            "factory:review",
            current_stage="factory:review",
        )
        == "factory:review"
    )


def test_primitives_do_not_rewrite_no_diff_exhaustion_as_blocked() -> None:
    """Release accounting stays in markers; factory:blocked remains a genuine blocker."""
    text = PRIMITIVES.read_text(encoding="utf-8")
    assert "quarantining" not in text
    assert "stage='factory:blocked'" not in text
    assert "comic-pile-factory-claim-released-v3" in text
    assert "no-persisted-change-handoff" in text
    assert "pr_no_diff_generation_fields" in text
    assert "sha=" in text


def _run_release_script(script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "FACTORY_WORKER": "45",
            "FACTORY_SOURCE": "opencode-free",
            "FACTORY_MODEL": "test-model",
            "FACTORY_RUNTIME_MODEL": "test-runtime",
            "FACTORY_NO_DIFF_RETRY_LIMIT": "1",
        }
    )
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def test_pr_no_diff_release_marker_pins_head_generation() -> None:
    """No-diff PR releases record the exact head, stage, and conflict state."""
    script = f"""
source <(sed '/^ensure_owner_label$/,$d' \"{PRIMITIVES}\")
marker_file="$(mktemp)"
current_stage() {{ printf '%s\\n' 'factory:changes-requested'; }}
replace_labels() {{ :; }}
git() {{ printf '%s\\n' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; }}
gh() {{
  if [[ \"$1\" == pr && \"$2\" == view ]]; then
    printf '%s\\n' '{{"headRefOid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}}'
    return 0
  fi
  if [[ \"$1\" == issue && \"$2\" == comment ]]; then
    printf '%s\\n' \"$5\" > \"$marker_file\"
    return 0
  fi
  return 0
}}
log() {{ :; }}
release_target 2010 factory:changes-requested repair-no-persisted-change-handoff pr
cat \"$marker_file\"
rm -f \"$marker_file\"
"""
    completed = _run_release_script(script)
    marker = completed.stdout.strip()
    assert "repair-no-persisted-change-handoff:sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in marker
    assert ":stage=factory:changes-requested:conflicted=0" in marker


def test_pr_no_diff_without_head_sha_retains_lease() -> None:
    """A PR no-diff release that cannot pin a head SHA must not count an unscoped retry."""
    script = f"""
source <(sed '/^ensure_owner_label$/,$d' \"{PRIMITIVES}\")
current_stage() {{ printf '%s\\n' 'factory:changes-requested'; }}
replace_labels() {{ printf 'relabeled\\n'; }}
git() {{ return 1; }}
gh() {{ return 0; }}
log() {{ printf '%s\\n' \"$*\"; }}
release_target 2010 factory:changes-requested repair-no-persisted-change-handoff pr || true
printf 'retain=%s\\n' \"${{FACTORY_RETAIN_LEASE_ON_EXIT:-0}}\"
"""
    completed = _run_release_script(script)
    assert "relabeled" not in completed.stdout
    assert "retain=1" in completed.stdout
    assert "unable to pin no-diff generation" in completed.stdout
