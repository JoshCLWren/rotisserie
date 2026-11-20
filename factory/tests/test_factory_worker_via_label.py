"""The readable via: label names the provider and model that last worked on a target."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / ".github/scripts/free-model-factory-worker-primitives.sh"


def _run(tmp_path: Path, source: str, model: str, current: str) -> tuple[str, list[str]]:
    log = tmp_path / "gh.log"
    log.unlink(missing_ok=True)
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [[ "$1 $2" == "issue view" ]]; then printf "%s" "$FAKE_CURRENT"; fi\n'
    )
    gh.chmod(0o755)
    script = (
        f'eval "$(sed -n "/^worker_via_label() {{/,/^}}/p;/^mark_worker_via() {{/,/^}}/p" "{PRIMITIVES}")"\n'
        f'SOURCE="{source}" MODEL="{model}"\n'
        'worker_via_label; echo; mark_worker_via 42\n'
    )
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "FAKE_CURRENT": current}
    out = subprocess.run(["bash", "-c", script], env=env, check=True, text=True,
                         capture_output=True).stdout.splitlines()[0]
    calls = log.read_text().splitlines() if log.exists() else []
    return out, calls


def test_label_names_provider_and_short_model_within_github_limit(tmp_path: Path) -> None:
    name, _ = _run(tmp_path, "openrouter-free", "meta-llama/llama-4-scout:free", "")
    assert name == "via:openrouter-free/llama-4-scout"
    long, _ = _run(tmp_path, "cloudflare-workers-ai", "@cf/" + "x" * 80, "")
    assert len(long) == 50 and long.startswith("via:cloudflare-workers-ai/")


def test_replaces_the_previous_via_label_and_skips_when_current(tmp_path: Path) -> None:
    _, calls = _run(tmp_path, "groq-free", "llama-3.3", "via:nvidia/old\nvia:z-ai/older")
    edit = next(call for call in calls if call.startswith("issue edit 42"))
    assert "--add-label via:groq-free/llama-3.3" in edit
    assert "--remove-label via:nvidia/old" in edit and "--remove-label via:z-ai/older" in edit
    _, calls = _run(tmp_path, "groq-free", "llama-3.3", "via:groq-free/llama-3.3")
    assert not any(call.startswith(("label create", "issue edit")) for call in calls)


WORKER = ROOT / ".github/scripts/free-model-factory-worker.sh"


def test_every_model_attempt_labels_its_task_before_running() -> None:
    text = WORKER.read_text()
    marker = 'log "FACTORY_EXECUTION_CANDIDATE: source=$SOURCE model=$MODEL runtime=$RUNTIME_MODEL"\n  mark_worker_via_task\n'
    assert marker in text
    assert text.index(marker) < text.index('run_agent "$MODE" "$NUMBER" "$agent_timeout"')
    assert 'FACTORY_CHECKPOINT_SOURCE="$SOURCE"' in text


def _task(tmp_path: Path, mode: str, fail: bool = False) -> tuple[str, list[str]]:
    log = tmp_path / "gh.log"
    log.unlink(missing_ok=True)
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        + ('exit 1\n' if fail else '')
    )
    gh.chmod(0o755)
    script = (
        f'eval "$(sed -n "/^worker_via_label() {{/,/^}}/p;/^mark_worker_via() {{/,/^}}/p;/^mark_worker_via_task() {{/,/^}}/p" "{PRIMITIVES}")"\n'
        'linked_issue_from_pr() { echo 4853; }\nsleep() { :; }\n'
        f'SOURCE=nvidia MODEL=meta/muse-glimmer-30b MODE={mode} NUMBER=5152\n'
        'mark_worker_via_task\n'
    )
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    out = subprocess.run(["bash", "-c", script], env=env, check=True, text=True,
                         capture_output=True).stdout
    return out, log.read_text().splitlines() if log.exists() else []


def test_pr_work_labels_the_pr_and_its_issue(tmp_path: Path) -> None:
    _, calls = _task(tmp_path, "pr")
    edits = [call for call in calls if call.startswith("issue edit")]
    assert [call.split()[2] for call in edits] == ["5152", "4853"]
    assert all("--add-label via:nvidia/muse-glimmer-30b" in call for call in edits)
    _, calls = _task(tmp_path, "issue")
    assert [call.split()[2] for call in calls if call.startswith("issue edit")] == ["5152"]


def test_label_failures_retry_and_warn(tmp_path: Path) -> None:
    out, calls = _task(tmp_path, "issue", fail=True)
    assert sum(call.startswith("issue view 5152") for call in calls) == 3
    assert "::warning::Could not set via:nvidia/muse-glimmer-30b on #5152" in out
