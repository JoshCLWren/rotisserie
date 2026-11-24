"""Run the factory store's trusted workflows (self-test, backup export) and read their results.

The libSQL store's credentials live only in the adopter's Actions secrets, so
this machine never connects to it. ``selftest`` starts the adopter's
``factory-turso-selftest.yml``, which runs the coordination test matrix against
the hosted database in a throwaway namespace and publishes a JSON summary.
``export`` starts ``factory-state-export.yml``, which copies the store into a
SQLite file, encrypts it with the adopter's ``FACTORY_EXPORT_KEY`` secret and
publishes it with a JSON summary; ``download`` saves the file here and decrypts
it with the same key from this machine's ``FACTORY_EXPORT_KEY``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from rotisserie.factory_console.github import GitHub, GitHubError, obj, string
from rotisserie.factory_console.service import bridge
from rotisserie.factory_console.tracker import Clock

WORKFLOWS = {"selftest": "factory-turso-selftest.yml", "export": "factory-state-export.yml"}
SUMMARIES = {
    "selftest": "factory-turso-selftest-{run_id}",
    "export": "factory-state-export-summary-{run_id}",
}
EXPORT_FILE = "factory-state-export-{run_id}"
KEY_VARIABLE = "FACTORY_EXPORT_KEY"
# Must match the encrypt step in .github/workflows/factory-state-export.yml.
CIPHER = ("-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-md", "sha256")
TIMEOUT = 1800.0


def _mode(mode: str) -> str:
    if mode not in WORKFLOWS:
        raise ValueError("mode is selftest or export")
    return mode


def start(github: GitHub, mode: str, *, measure: bool = False) -> dict[str, object]:
    inputs = {"measure": "true"} if measure and _mode(mode) == "selftest" else {}
    run_id, url = bridge._start(github, WORKFLOWS[_mode(mode)], inputs)
    return {"status": "started", "mode": mode, "bridge": {"run_id": run_id, "url": url}}


def run(
    github: GitHub,
    clock: Clock,
    mode: str,
    *,
    measure: bool = False,
    started: dict[str, object] | None = None,
) -> dict[str, object]:
    inputs = {"measure": "true"} if measure and _mode(mode) == "selftest" else {}
    result = bridge.run(
        github,
        clock,
        WORKFLOWS[_mode(mode)],
        inputs,
        artifact=SUMMARIES[mode],
        timeout=TIMEOUT,
        started=started,
    )
    return {**result.data, "mode": mode, "bridge": result.summary()}


def earlier(github: GitHub, mode: str, run_id: int) -> dict[str, object]:
    """An earlier run's summary, or ``{"result": "pending"}`` while it still runs."""
    found = obj(github.repo_api(f"actions/runs/{run_id}"))
    row: dict[str, object] = {
        "run_id": run_id,
        "url": string(found, "html_url"),
        "status": string(found, "status"),
        "conclusion": string(found, "conclusion"),
    }
    if row["status"] != "completed":
        return {"result": "pending", "mode": mode, "bridge": row}
    name = SUMMARIES[_mode(mode)].format(run_id=run_id)
    data = bridge._artifact(github, run_id, name, str(row["url"]))
    return {**data, "mode": mode, "bridge": row}


def decrypt(encrypted: Path, destination: Path, key: str) -> None:
    """Decrypt an export with the adopter's key; the file is readable only by this user."""
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            result = subprocess.run(
                [
                    "openssl",
                    "enc",
                    "-d",
                    *CIPHER,
                    "-pass",
                    f"env:{KEY_VARIABLE}",
                    "-in",
                    str(encrypted),
                ],
                env={**os.environ, KEY_VARIABLE: key},
                stdout=target,
                stderr=subprocess.PIPE,
                check=False,
            )
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise bridge.BridgeError(f"openssl could not run: {exc}") from exc
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise bridge.BridgeError("the export could not be decrypted: check FACTORY_EXPORT_KEY")


def download(github: GitHub, run_id: int, destination: Path, *, key: str | None = None) -> Path:
    """Save and decrypt an export run's SQLite file; refuses to overwrite an existing file."""
    if destination.exists():
        raise FileExistsError(f"{destination} already exists")
    key = key if key is not None else os.environ.get(KEY_VARIABLE, "")
    if not key:
        raise bridge.BridgeError(f"set {KEY_VARIABLE} to the export key to decrypt the file")
    with tempfile.TemporaryDirectory(prefix="rotisserie-store-") as folder:
        try:
            github.gh(
                "run",
                "download",
                str(run_id),
                "--repo",
                github.repo,
                "--name",
                EXPORT_FILE.format(run_id=run_id),
                "--dir",
                folder,
            )
        except GitHubError as exc:
            raise bridge.BridgeError(f"the export file could not be downloaded: {exc}") from exc
        found = sorted(Path(folder).rglob("*.sqlite.enc"))
        if len(found) != 1:
            raise bridge.BridgeError("the export artifact holds no single encrypted SQLite file")
        decrypt(found[0], destination, key)
    return destination
