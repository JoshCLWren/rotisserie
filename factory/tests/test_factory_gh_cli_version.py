"""Regression coverage for the factory GitHub CLI compatibility setup."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
GH_VERSION = "2.97.0"
GH_LINUX_AMD64_SHA256 = "a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112"


def test_factory_gate_workflows_ensure_json_capable_github_cli() -> None:
    """Every runtime that invokes mechanical gates installs a JSON-capable gh first."""
    for name in ("fixed-model-factory-dispatch.yml", "factory-ready-merge-drain.yml"):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        setup = text.index("Ensure GitHub CLI supports JSON PR checks")
        drain = text.index("Drain exact-head ready factory PRs")

        assert setup < drain
        assert "gh pr checks --help" in text
        assert f"version='{GH_VERSION}'" in text
        assert GH_LINUX_AMD64_SHA256 in text
        assert "sha256sum --check --strict" in text


def test_dispatcher_selects_upgraded_gh_before_installing_rest_shim() -> None:
    """The dispatcher shim delegates to the upgraded CLI, not the runner's old binary."""
    text = (WORKFLOW_DIR / "fixed-model-factory-dispatch.yml").read_text(encoding="utf-8")

    setup = text.index("Ensure GitHub CLI supports JSON PR checks")
    shim = text.index("Install REST-backed factory GitHub reads")
    assert setup < shim


def test_help_probes_consume_large_output_without_hiding_failures(tmp_path: Path) -> None:
    """Real workflow pipelines tolerate large help but reject missing JSON or failed gh."""
    import subprocess

    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "print(os.environ.get('HELP_FLAG', '--json'), flush=True)\n"
        "for _ in range(1000):\n"
        "    print('help text ' * 100, flush=True)\n"
        "sys.exit(int(os.environ.get('HELP_EXIT', '0')))\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    for name in ("fixed-model-factory-dispatch.yml", "factory-ready-merge-drain.yml"):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines() if "pr checks --help" in line]
        assert len(lines) == 2
        for line in lines:
            pipeline = line.removeprefix("if ").removesuffix("; then")
            script = 'set -euo pipefail\nexport PATH="$PROBE_BIN:$PATH"\n' + pipeline
            for flag, exit_code, expected in (("--json", "0", 0), ("--other", "0", 1), ("--json", "23", 23)):
                import os

                result = subprocess.run(
                    ["bash", "-c", script],
                    env={**os.environ, "PROBE_BIN": str(tmp_path), "gh_dir": str(tmp_path), "HELP_FLAG": flag, "HELP_EXIT": exit_code},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert result.returncode == expected, result.stderr
