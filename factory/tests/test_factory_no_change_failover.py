"""A model that makes no change is replaced inside the same worker run."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / ".github/scripts/free-model-factory-worker.sh"


def _eligible(tmp_path: Path, *, mode: str = "pr", stage: str = "factory:changes-requested",
              dirty: str = "", head: str = "base", verdict: str = "", remaining: int = 3000,
              source: str = "nvidia", blocked: str = "") -> bool:
    text = WORKER.read_text()
    func = re.search(r"^no_change_failover_eligible\(\) \{.*?^\}", text, re.S | re.M)
    assert func
    script = f"""
git() {{ if [[ "$1" == status ]]; then printf '%s' '{dirty}'; else echo {head}; fi; }}
factory_extract_semantic_verdict() {{ echo '{verdict}'; }}
remaining() {{ echo {remaining}; }}
SOURCE={source} MODE={mode} ASSIGNED_PR_STAGE={stage} EXPECTED_HEAD=base WORKER=1
FACTORY_PUSH_BLOCKED_REASON='{blocked}'
{func[0]}
no_change_failover_eligible
"""
    return subprocess.run(["bash", "-c", script], cwd=tmp_path).returncode == 0


def test_clean_untouched_repair_or_issue_moves_to_the_next_model(tmp_path: Path) -> None:
    assert _eligible(tmp_path)
    assert _eligible(tmp_path, mode="issue", stage="")


def test_real_work_ready_verdicts_and_reviews_keep_the_normal_path(tmp_path: Path) -> None:
    assert not _eligible(tmp_path, dirty=" M app.py")
    assert not _eligible(tmp_path, head="moved")
    assert not _eligible(tmp_path, verdict="approve")
    assert not _eligible(tmp_path, stage="factory:review")
    assert not _eligible(tmp_path, blocked="failed-validation")
    assert not _eligible(tmp_path, remaining=500)
    assert not _eligible(tmp_path, source="omniroute-free")


def test_no_change_failover_never_backs_off_the_model() -> None:
    text = WORKER.read_text()
    start = text.index("if no_change_failover_eligible; then")
    branch = text[start:text.index("    break\n  fi\n", start)]
    assert "FACTORY_NO_CHANGE_FAILOVER" in branch
    assert "attempted_pairs+=" in branch and "select_next_eligible_model" in branch
    assert "record_model_failover_failure" not in branch
    assert text.index("if no_change_failover_eligible; then") < text.index("record_agent_failure_outcome \"$agent_status\"\n  failure_class")
