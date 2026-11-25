"""The repair guard keeps real repairs: named files and tests for changed code count as overlap."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

GUARD = Path(__file__).resolve().parents[1] / ".github/scripts/fixed-model-guard.py"
PR_5773 = {
    "frontend/src/components/ComicIssueCard.tsx",
    "frontend/src/components/ComicIssueReadingCard.tsx",
    "frontend/src/components/CompactIssueCardTrigger.tsx",
    "frontend/src/components/ReaderIssueActions.tsx",
}
PR_5767 = {"native/contracts/README.md", "native/contracts/roll-bootstrap-v1.openapi.json"}


def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fixed_model_guard_scope", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decide(previous: set[str], latest: set[str], named: str = "") -> dict[str, object]:
    return guard().reject_decision(previous, latest, title="Reader cards", body="", named=named)


def test_the_three_test_files_run_45594349578_lost_are_kept() -> None:
    lost = (
        "frontend/src/unit/CompactIssueCardTrigger.test.tsx",
        "frontend/src/unit/HistoryPage.issue-card.test.tsx",
        "frontend/src/unit/ReadingListReader.test.tsx",
    )
    assert decide(PR_5773, set(lost))["reject"] is False
    for path in lost:
        assert decide(PR_5773, {path})["reject"] is False, path


def test_test_files_beside_or_above_the_changed_components_are_kept() -> None:
    latest = {
        "frontend/src/components/__tests__/ComicIssueCard.test.tsx",
        "frontend/tests/reader-issue-actions.spec.ts",
    }
    assert decide(PR_5773, latest)["reject"] is False


def test_a_test_for_unrelated_code_is_still_rejected() -> None:
    decision = decide(PR_5773, {"backend/tests/test_billing.py"})
    assert decision["reason"] == "zero-overlap-with-existing-pr-diff"


def test_a_file_the_findings_name_is_kept() -> None:
    named = "- frontend/src/hooks/useComicIssueMapping.ts:42 drops the late response"
    assert decide(PR_5773, {"frontend/src/hooks/useComicIssueMapping.ts"}, named)["reject"] is False
    assert decide(PR_5773, {"frontend/src/hooks/useComicIssueMapping.ts"})["reject"] is True


def test_a_junk_marker_file_is_rejected_even_when_named() -> None:
    decision = decide(PR_5767, {"FACTORY_ACCEPTANCE_V1"}, named="FACTORY_ACCEPTANCE_V1 report")
    assert decision["reason"] == "factory-marker-file"


def test_a_factory_control_file_is_never_excused_by_findings() -> None:
    path = ".github/scripts/free-model-factory-worker.sh"
    decision = decide(PR_5773, {path}, named=path)
    assert decision["reject"] is True


def test_the_cli_reads_the_findings_file(tmp_path: Path) -> None:
    findings = tmp_path / "findings.md"
    findings.write_text("frontend/src/hooks/useComicIssueMapping.ts:42 is wrong\n")
    out = subprocess.run(
        [
            sys.executable,
            str(GUARD),
            "--previous-json",
            json.dumps(sorted(PR_5773)),
            "--latest-json",
            json.dumps(["frontend/src/hooks/useComicIssueMapping.ts"]),
            "--findings-file",
            str(findings),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout)["reject"] is False


def test_the_self_test_still_passes() -> None:
    proc = subprocess.run([sys.executable, str(GUARD), "--self-test"], capture_output=True)
    assert proc.returncode == 0
