"""Adopted checkpoint snapshots must not look like a model rewriting the PR head."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / ".github/scripts/free-model-factory-worker-primitives.sh"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          capture_output=True).stdout.strip()


def repo(tmp_path: Path) -> tuple[Path, str]:
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("base\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    return work, git(work, "rev-parse", "HEAD")


def checkpoint(work: Path, parent: str, text: str) -> str:
    (work / "a.txt").write_text(text)
    git(work, "add", "-A")
    tree = git(work, "write-tree")
    sha = subprocess.run(
        ["git", "commit-tree", tree, "-p", parent, "-m",
         "checkpoint(factory): worker 1 run 2 pr #3 (persisted work; validation pending)"],
        cwd=work, check=True, text=True, capture_output=True).stdout.strip()
    git(work, "reset", "-q", "--mixed", sha)
    return sha


def state(work: Path, base: str, saved: str = "") -> dict:
    ref = work.parent / "ref"
    ref.write_text(f"{saved}\nfactory/work-checkpoints/x\n" if saved else "")
    script = (
        f'eval "$(sed -n "/^head_moved_by_model() {{/,/^}}/p;/^unclean_git_state_json() {{/,/^}}/p" "{PRIMITIVES}")"\n'
        "conflict_markers_present() { return 1; }\nbranch_folded_main_commits() { return 1; }\n"
        f"EXPECTED_HEAD={base} CHECKPOINT_REF_FILE={ref}\nunclean_git_state_json\n"
    )
    out = subprocess.run(["bash", "-c", script], cwd=work, check=True, text=True,
                         capture_output=True).stdout
    return json.loads(out)


def test_adopted_checkpoint_chain_is_not_a_head_change(tmp_path: Path) -> None:
    work, base = repo(tmp_path)
    first = checkpoint(work, base, "one\n")
    second = checkpoint(work, first, "two\n")
    assert state(work, base, second)["head_changed"] is False


def test_model_commits_and_unrecorded_heads_still_count(tmp_path: Path) -> None:
    work, base = repo(tmp_path)
    saved = checkpoint(work, base, "one\n")
    assert state(work, base)["head_changed"] is True
    (work / "a.txt").write_text("model\n")
    git(work, "commit", "-qam", "model commit")
    assert state(work, base, saved)["head_changed"] is True
    assert state(work, base, git(work, "rev-parse", "HEAD"))["head_changed"] is True


def test_unmoved_head_is_clean(tmp_path: Path) -> None:
    work, base = repo(tmp_path)
    assert state(work, base)["head_changed"] is False
