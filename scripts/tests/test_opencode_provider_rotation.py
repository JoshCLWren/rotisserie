"""Regression coverage for live OpenCode provider rotation."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[2]


class ProviderRotationTests(unittest.TestCase):
    """Verify direct factory runs discover every usable OpenCode provider."""

    def test_direct_run_refreshes_providers_before_rotation(self) -> None:
        """Refresh only curated free-model providers before rotating heartbeats."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = root / "scripts"
            bin_dir = root / "bin"
            state = root / "state"
            scripts.mkdir()
            bin_dir.mkdir()
            state.mkdir()

            for name in (
                "comic-pile-opencode-factory.sh",
                "opencode-model-manifest.sh",
            ):
                target = scripts / name
                shutil.copy2(SOURCE_ROOT / "scripts" / name, target)
                target.chmod(0o755)

            scout = scripts / "opencode-model-scout.sh"
            scout.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    state=""
                    candidates=""
                    while (($#)); do
                      case "$1" in
                        --state-dir) state="$2"; shift 2 ;;
                        --candidates-file) candidates="$2"; shift 2 ;;
                        *) shift ;;
                      esac
                    done
                    while IFS= read -r model; do
                      [[ -n "$model" ]] || continue
                      "$(dirname "$0")/opencode-model-manifest.sh" \
                        set "$model" confirmed yes "$state"
                    done <"$candidates"
                    """
                )
            )
            scout.chmod(0o755)

            heartbeat = scripts / "comic-pile-opencode-factory-heartbeat.sh"
            heartbeat.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    state=""
                    model=""
                    while (($#)); do
                      case "$1" in
                        --state-dir) state="$2"; shift 2 ;;
                        --model) model="$2"; shift 2 ;;
                        --help) exit 0 ;;
                        *) shift ;;
                      esac
                    done
                    printf '%s\n' "$model" >>"$state/models-used.log"
                    if [[ "$model" == cerebras/* ]]; then
                      exit 7
                    fi
                    printf 'FACTORY_RESULT: idle\n'
                    """
                )
            )
            heartbeat.chmod(0o755)

            opencode = bin_dir / "opencode"
            opencode.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    [[ "${1:-}" == "models" ]] || exit 2
                    printf '%s\n' \
                      cerebras/gemma \
                      deepseek/deepseek-v4-flash \
                      nvidia/nemotron \
                      opencode/zen \
                      fcm-nvidia/step \
                      openrouter/vendor/free-model:free \
                      openrouter/vendor/paid-model \
                      openrouter/openrouter/free \
                      google/gemini-3.1-flash-lite \
                      google/gemini-2.5-flash-lite \
                      google/gemini-2.5-pro \
                      nvidia/text-embedding
                    """
                )
            )
            opencode.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{bin_dir}:{env['PATH']}",
                    "COMIC_PILE_FACTORY_STATE_DIR": str(state),
                    "COMIC_PILE_DEFAULT_MODEL": "cerebras/gemma",
                    "FACTORY_MAX_FAILURES": "1",
                    "SCOUT_PARALLEL": "1",
                }
            )
            result = subprocess.run(
                [scripts / "comic-pile-opencode-factory.sh"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            used = (state / "models-used.log").read_text().splitlines()
            self.assertEqual(used, ["fcm-nvidia/step"])
            self.assertNotIn("cerebras/gemma", used)

            manifest = (state / "model_manifest.tsv").read_text()
            for curated in (
                "nvidia/nemotron\tconfirmed",
                "opencode/zen\tconfirmed",
                "fcm-nvidia/step\tconfirmed",
                "openrouter/vendor/free-model:free\tconfirmed",
                "openrouter/openrouter/free\tconfirmed",
                "google/gemini-3.1-flash-lite\tconfirmed",
                "google/gemini-2.5-flash-lite\tconfirmed",
            ):
                self.assertIn(curated, manifest)
            for excluded in (
                "cerebras/gemma",
                "deepseek/deepseek-v4-flash",
                "openrouter/vendor/paid-model",
                "google/gemini-2.5-pro",
                "nvidia/text-embedding",
            ):
                self.assertNotIn(excluded, manifest)
            self.assertIn(
                "Refreshing OpenCode model manifest from all available providers",
                result.stdout,
            )


if __name__ == "__main__":
    unittest.main()
