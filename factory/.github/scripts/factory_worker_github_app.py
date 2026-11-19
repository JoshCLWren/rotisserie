#!/usr/bin/env python3
"""Worker 48 GitHub App identity mapping and installation-token minting.

The provenance key is the worker number. This module configures worker 48
(Mark Cordova) only. It does not create Apps and it does not move any other
worker off PR_REBASE_TOKEN.

Name bootstrap is explicit (`bootstrap-name` / `bootstrap-collision`).
Token selection never calls Faker and never re-rolls a persisted name.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PILOT_WORKER = "48"
FAKER_VERSION = "40.40.0"
FAKER_LOCALE = "en_US"
TEST_KEY_SENTINEL = "TEST-ONLY-NOT-A-KEY"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MAPPING = REPO_ROOT / ".github" / "factory-worker-github-apps.json"


def mapping_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the persisted worker→App mapping, honoring a test override."""
    chosen = (env if env is not None else os.environ).get("FACTORY_WORKER_APP_MAPPING")
    if chosen:
        return Path(chosen)
    return DEFAULT_MAPPING


def load_mapping(path: Path | None = None) -> dict[str, Any]:
    """Load the persisted mapping. Missing file is an empty scaffold."""
    target = path or mapping_path()
    if not target.is_file():
        return {"workers": {}}
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"worker App mapping {target} must be a JSON object")
    return data


def _present(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"", "null", "none"}:
        return ""
    return text


def worker_entry(mapping: Mapping[str, Any], worker: str) -> dict[str, Any] | None:
    """Return the persisted entry for a pilot worker, else None."""
    if str(worker) != PILOT_WORKER:
        return None
    workers = mapping.get("workers")
    if not isinstance(workers, dict):
        return None
    entry = workers.get(PILOT_WORKER)
    return entry if isinstance(entry, dict) else None


def credential_decision(
    worker: str,
    env: Mapping[str, str],
    mapping: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Choose installation vs PR_REBASE_TOKEN without minting or renaming.

    Only worker 48 can select an installation token, and only when the
    persisted entry plus App id, installation id, and private key are present.
    Everyone else, including a fully configured non-48 row, stays on the
    shared producer.
    """
    loaded = load_mapping() if mapping is None else mapping
    decision = {"source": "pr_rebase", "worker": str(worker)}
    entry = worker_entry(loaded, str(worker))
    if entry is None:
        return decision
    app_id_secret = _present(entry.get("app_id_secret")) or "FACTORY_WORKER_48_APP_ID"
    installation_secret = (
        _present(entry.get("installation_id_secret")) or "FACTORY_WORKER_48_INSTALLATION_ID"
    )
    key_secret = _present(entry.get("private_key_secret")) or "FACTORY_WORKER_48_APP_PRIVATE_KEY"
    app_id = _present(entry.get("app_id")) or _present(env.get(app_id_secret))
    installation_id = _present(entry.get("installation_id")) or _present(env.get(installation_secret))
    private_key = _present(env.get(key_secret))
    if not app_id or not installation_id or not private_key:
        return decision
    decision["source"] = "installation"
    decision["app_id"] = app_id
    decision["installation_id"] = installation_id
    decision["private_key"] = private_key
    return decision


def _require_faker() -> None:
    import importlib.metadata

    installed = importlib.metadata.version("Faker")
    if installed != FAKER_VERSION:
        raise RuntimeError(
            f"refusing to bootstrap worker App names with Faker {installed}; need {FAKER_VERSION}"
        )


def faker_names(worker: int, calls: int) -> list[str]:
    """Return the first `calls` name() results from one seeded Faker instance."""
    if calls < 1:
        raise ValueError("calls must be positive")
    _require_faker()
    from faker import Faker

    faker = Faker(FAKER_LOCALE)
    faker.seed_instance(int(worker) + 100)
    return [faker.name() for _ in range(calls)]


def bootstrap_display_name(worker: int) -> str:
    """First name() call. This is the persisted display name for a new worker."""
    return faker_names(worker, 1)[0]


def collision_fallback_name(worker: int) -> str:
    """Second name() on the same seeded instance. Creation-time only; then stop."""
    return faker_names(worker, 2)[1]


def normalize_pem(value: str) -> str:
    """Accept a PEM block or a one-line secret with escaped newlines."""
    text = value.strip()
    if "\\n" in text and "-----BEGIN" in text:
        text = text.replace("\\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    """Decode a JWT segment without requiring padding."""
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def build_app_jwt(app_id: str, private_key_pem: str, now: int | None = None) -> str:
    """Sign a short-lived GitHub App JWT (RS256) with the App private key."""
    issued = int(time.time() if now is None else now)
    header = {"alg": "RS256", "typ": "JWT"}
    iss: int | str = int(app_id) if str(app_id).isdigit() else str(app_id)
    payload = {"iat": issued - 60, "exp": issued + 9 * 60, "iss": iss}
    signing_input = ".".join(
        (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        )
    )
    pem = normalize_pem(private_key_pem)
    key_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(pem)
            key_path = handle.name
        os.chmod(key_path, 0o600)
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path],
            input=signing_input.encode("ascii"),
            capture_output=True,
            check=False,
        )
    finally:
        if key_path:
            os.unlink(key_path)
    if proc.returncode != 0:
        raise RuntimeError("openssl failed to sign the GitHub App JWT")
    return f"{signing_input}.{_b64url(proc.stdout)}"


def mint_installation_token(
    *,
    app_id: str,
    installation_id: str,
    private_key_pem: str,
    opener: Any = None,
    now: int | None = None,
) -> str:
    """Exchange the App JWT for a short-lived installation token.

    The test sentinel never reaches GitHub. A real PEM is signed locally and
    posted to the installation access-token endpoint. The private key is not
    logged.
    """
    if private_key_pem.strip() == TEST_KEY_SENTINEL:
        token = os.environ.get("FACTORY_WORKER_APP_MINT_STUB_TOKEN", "").strip()
        if not token:
            raise RuntimeError("refusing to mint from the test-only private key sentinel")
        return token
    jwt = build_app_jwt(app_id, private_key_pem, now=now)
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "comic-pile-factory-worker-app",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"GitHub installation token request failed ({exc.code})"
        ) from exc
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("GitHub installation token response did not include a token")
    return token


def resolve_push_credential(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return (source, token) for push and PR creation."""
    current = env if env is not None else os.environ
    decision = credential_decision(str(current.get("FACTORY_WORKER", "")), current)
    if decision["source"] != "installation":
        token = _present(current.get("PR_REBASE_TOKEN"))
        if not token:
            raise RuntimeError("PR_REBASE_TOKEN is required for trusted PR creation")
        return "pr_rebase", token
    token = mint_installation_token(
        app_id=decision["app_id"],
        installation_id=decision["installation_id"],
        private_key_pem=decision["private_key"],
    )
    return "installation", token


HANDOFF_KINDS = ("implementation", "repair", "review")
_MARKER_FRAGMENTS = ("<!--", "-->", "comic-pile-factory-", "free-model-factory-owner")


def handoff_decision(
    worker: str,
    env: Mapping[str, str],
    mapping: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Choose the author of a human-readable handoff comment.

    Same gate as push and PR creation: only worker 48 with the persisted entry
    plus App id, installation id, and private key gets ``installation``.
    Everyone else gets ``default``, which means the caller keeps its current
    identity and posts nothing new. A handoff never falls back to
    PR_REBASE_TOKEN, and machine markers never route through this decision.
    """
    decision = credential_decision(worker, env, mapping)
    if decision["source"] != "installation":
        return {"source": "default", "worker": str(worker)}
    return decision


def resolve_handoff_credential(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return (source, token) for a readable handoff. Token is empty for default."""
    current = env if env is not None else os.environ
    decision = handoff_decision(str(current.get("FACTORY_WORKER", "")), current)
    if decision["source"] != "installation":
        return "default", ""
    token = mint_installation_token(
        app_id=decision["app_id"],
        installation_id=decision["installation_id"],
        private_key_pem=decision["private_key"],
    )
    return "installation", token


def sanitize_handoff_text(text: str) -> str:
    """Strip anything that could look like a trusted factory marker.

    The App-authored comment is plain prose for humans. Trust logic ignores
    worker App authors anyway; this keeps the comment from even resembling
    a marker so nobody mistakes it for controller state.
    """
    cleaned = str(text)
    while "<!--" in cleaned:
        start = cleaned.index("<!--")
        end = cleaned.find("-->", start)
        cleaned = cleaned[:start] + (cleaned[end + 3 :] if end != -1 else "")
    cleaned = cleaned.replace("-->", "")
    lines = [
        line
        for line in cleaned.splitlines()
        if not any(fragment in line for fragment in _MARKER_FRAGMENTS)
    ]
    return "\n".join(lines).strip()


def render_handoff(
    *,
    kind: str,
    worker: str,
    display: str = "",
    model: str = "",
    head: str = "",
    detail: str = "",
) -> str:
    """Render a short human-readable handoff with no machine marker."""
    if kind not in HANDOFF_KINDS:
        raise ValueError(f"unknown handoff kind: {kind}")
    head_text = f" at `{head[:12]}`" if head else ""
    if kind == "implementation":
        headline = f"Factory worker {worker} opened this pull request{head_text} and handed it to review."
    elif kind == "repair":
        headline = f"Factory worker {worker} pushed repairs{head_text} and handed this pull request back to review."
    else:
        headline = f"Factory worker {worker} finished a semantic review{head_text}."
    lines = [f"### Factory handoff · {kind}", "", headline]
    if detail:
        lines.extend(["", sanitize_handoff_text(detail)])
    meta = []
    if display:
        meta.append(f"Worker: {display}")
    if model:
        meta.append(f"Model: {model}")
    if meta:
        lines.extend(["", " · ".join(meta)])
    lines.extend(
        [
            "",
            "This note is for people reading the thread. It is not controller state, "
            "and it is not a review or merge decision. Trusted factory records stay on "
            "github-actions[bot].",
        ]
    )
    body = sanitize_handoff_text("\n".join(lines))
    if any(fragment in body for fragment in ("<!--", "-->")):
        raise RuntimeError("readable handoff must not contain a machine marker")
    return body + "\n"


def _flag(argv: list[str], name: str) -> str:
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return ""


def main(argv: list[str]) -> int:
    """CLI used by the factory worker shell. Tokens go to stdout only."""
    if len(argv) < 2:
        print("usage: factory_worker_github_app.py resolve|select-source|resolve-handoff|render-handoff|bootstrap-name|bootstrap-collision", file=sys.stderr)
        return 2
    command = argv[1]
    if command == "select-source":
        decision = credential_decision(os.environ.get("FACTORY_WORKER", ""), os.environ)
        print(decision["source"])
        return 0
    if command == "resolve":
        try:
            source, token = resolve_push_credential(os.environ)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        sys.stdout.write(f"{source}\t{token}\n")
        return 0
    if command == "resolve-handoff":
        # Best effort. A mint failure keeps the current identity and posts nothing.
        try:
            source, token = resolve_handoff_credential(os.environ)
        except RuntimeError as exc:
            print(f"worker App handoff token unavailable: {exc}", file=sys.stderr)
            source, token = "default", ""
        sys.stdout.write(f"{source}\t{token}\n")
        return 0
    if command == "render-handoff":
        try:
            body = render_handoff(
                kind=_flag(argv, "--kind"),
                worker=_flag(argv, "--worker") or os.environ.get("FACTORY_WORKER", ""),
                display=_flag(argv, "--display"),
                model=_flag(argv, "--model"),
                head=_flag(argv, "--head"),
                detail=_flag(argv, "--detail"),
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        sys.stdout.write(body)
        return 0
    if command in {"bootstrap-name", "bootstrap-collision"}:
        if len(argv) != 3 or not argv[2].isdigit():
            print(f"usage: {command} WORKER_NUMBER", file=sys.stderr)
            return 2
        worker = int(argv[2])
        try:
            name = bootstrap_display_name(worker) if command == "bootstrap-name" else collision_fallback_name(worker)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(name)
        return 0
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
