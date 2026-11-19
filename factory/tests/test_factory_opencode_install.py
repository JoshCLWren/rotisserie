"""Exercise the production OpenCode installer without relying on a live npm registry."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / ".github/scripts/install-opencode-v2.sh"


def run_installer(tmp_path: Path, *, all_fail: bool = False) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['view', '@opencode/cli']:\n"
        "    print('[\"2.2.5\", \"2.2.6\", \"2.2.7\"]')\n"
        "    sys.exit(0)\n"
        "if args[:2] != ['install', '--global']:\n"
        "    sys.exit(8)\n"
        "with open(os.environ['NPM_ATTEMPTS'], 'a') as out:\n"
        "    out.write(args[-1] + '\\n')\n"
        "if os.environ.get('ALL_FAIL') == '1' or args[-1].endswith('2.2.7'):\n"
        "    print('platform tarball 404', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "prefix = pathlib.Path(args[args.index('--prefix') + 1])\n"
        "binary = prefix / 'bin/opencode'\n"
        "binary.parent.mkdir(parents=True, exist_ok=True)\n"
        "binary.write_text('#!/bin/sh\\necho 2.2.6\\n')\n"
        "binary.chmod(0o755)\n"
    )
    npm.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "HOME": str(tmp_path / "home"),
        "GITHUB_PATH": str(tmp_path / "github_path"),
        "NPM_ATTEMPTS": str(tmp_path / "attempts"),
        "ALL_FAIL": "1" if all_fail else "0",
    }
    return subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_unpublished_latest_platform_package_falls_back_to_previous_v2(tmp_path: Path) -> None:
    result = run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attempts").read_text().splitlines() == [
        "@opencode/cli@2.2.7",
        "@opencode/cli@2.2.6",
    ]
    assert (tmp_path / "github_path").read_text().strip().endswith("comic-pile-opencode-v2/bin")
    assert (tmp_path / "home/.config/opencode/opencode.json").is_file()


def test_exhausted_published_versions_fail_closed_without_fake_success(tmp_path: Path) -> None:
    result = run_installer(tmp_path, all_fail=True)
    assert result.returncode != 0
    assert "No working published OpenCode v2 CLI" in result.stderr
    assert (tmp_path / "attempts").read_text().splitlines() == [
        "@opencode/cli@2.2.7",
        "@opencode/cli@2.2.6",
        "@opencode/cli@2.2.5",
    ]
    assert not (tmp_path / "github_path").exists()
