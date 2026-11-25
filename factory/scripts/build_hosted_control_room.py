#!/usr/bin/env python3
"""Publish the existing local Rotisserie control-room UI on GitHub Pages.

The generated HTML is deliberately a static client. Public reads come only
from sanitized status snapshots; operator writes go through an authenticated
GitHub Actions dispatch, not directly to privileged factory credentials.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "src" / "rotisserie" / "web_ui.html"
SCRIPT = ROOT / "src" / "rotisserie" / "web_ui.js"
SCRIPT_TAG = '<script src="/web_ui.js"></script>'
CONTRACT = ROOT / "src" / "rotisserie" / "control_contract.py"
ADAPTER = Path(__file__).with_name("hosted_control_room.js")
PARTS = tuple(
    Path(__file__).parent / "hosted" / name for name in ("public_reads.js", "session.js", "live.js")
)
REQUEST = re.compile(r"(?m)^async function request\(path,body\)\{[^\n]*\}$")
SHELL_RELOAD_TRAP = "<!-- legacy shell reload target: </head> -->"
AUTH_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9._~/-]*)?")


def configured_auth_url() -> str:
    """The adopter's dashboard sign-in service, from [dashboard] auth_url."""
    path = Path(os.environ.get("ROTISSERIE_CONFIG") or ".rotisserie/config.toml")
    try:
        with path.open("rb") as handle:
            value = (tomllib.load(handle).get("dashboard") or {}).get("auth_url", "")
    except FileNotFoundError:
        return ""
    return value if isinstance(value, str) else ""


def contract_tables() -> dict[str, object]:
    """The shared command contract, loaded by path: the Pages job installs nothing."""
    spec = importlib.util.spec_from_file_location("rotisserie_control_contract", CONTRACT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Rotisserie control contract not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        "public_reads": sorted(module.PUBLIC_READS),
        "live_reads": sorted(module.LIVE_READS),
        "write_operations": sorted(module.WRITE_OPERATIONS),
        "max_batch": module.MAX_BATCH,
    }


def adapter(auth_url: str) -> str:
    script = "\n".join(part.read_text(encoding="utf-8") for part in (*PARTS, ADAPTER))
    script = script.replace('"__AUTH_BASE__"', json.dumps(auth_url))
    tables = json.dumps(contract_tables())
    return f'"use strict";\n(function () {{\nconst CONTRACT = {tables};\n{script}\n}})();'


def render(repo: str, auth_url: str = "") -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
        raise ValueError("Invalid repository name")
    auth_url = auth_url.rstrip("/")
    if auth_url and not AUTH_URL.fullmatch(auth_url):
        raise ValueError("Dashboard sign-in URL must be a plain https URL")
    auth_origin = "/".join(auth_url.split("/")[:3]) if auth_url else ""
    page = UI.read_text(encoding="utf-8")
    page = page.replace("__SESSION_TOKEN__", "null").replace("__REPOSITORY__", json.dumps(repo))
    script, count = REQUEST.subn(
        "async function request(path,body){return window.hostedRequest(path,body)}",
        SCRIPT.read_text(encoding="utf-8"),
    )
    if count != 1 or page.count(SCRIPT_TAG) != 1 or not script.startswith('"use strict";'):
        raise RuntimeError("Local control-room request contract changed; refusing to publish")
    # The hosted CSP allows inline scripts only, so the page carries its script.
    page = page.replace(
        SCRIPT_TAG,
        "<script>\n" + adapter(auth_url) + "\n</script>\n<script>\n" + script + "</script>",
        1,
    )
    page = page.replace("Local operator session", "Hosted factory dashboard")
    page = page.replace(
        "GitHub via your existing gh login",
        "GitHub authorizes remote operations. Read-only status is public.",
    )
    page = page.replace(
        "Served locally · All mutations pass through existing factory commands",
        "GitHub Pages · Published snapshot for public views · "
        "Live views and operator actions run in GitHub Actions after sign-in",
    )
    page = page.replace(
        "</aside>",
        '<div class="sidebar-bottom" id="hosted-admin">'
        "<strong>GitHub operator access</strong>"
        '<p class="hint">Use your existing GitHub account. '
        "Only approved administrators can control the factory.</p>"
        '<button class="button small" id="github-connect">Sign in with GitHub</button>'
        '<div class="hint" id="github-identity">Read-only visitor</div>'
        '<div class="hint" id="hosted-live" aria-live="polite"></div>'
        "</div></aside>",
        1,
    )
    page = page.replace(
        "</head>",
        '<meta name="referrer" content="no-referrer">'
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src &#39;none&#39;; script-src &#39;unsafe-inline&#39;; '
        "style-src &#39;unsafe-inline&#39;; connect-src https://raw.githubusercontent.com"
        + (f" {auth_origin}" if auth_origin else "")
        + "; img-src data:; base-uri &#39;none&#39;; "
        'form-action &#39;none&#39;; frame-ancestors &#39;none&#39;">'
        "</head>",
        1,
    )
    # Older Pages shells insert a 60 second reload before the first "</head>". That
    # reload drops live reads and operator runs, so it must land inside this comment.
    return page.replace("<head>", "<head>\n" + SHELL_RELOAD_TRAP, 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--auth-url", default=None, help="defaults to [dashboard] auth_url")
    args = parser.parse_args()
    auth_url = configured_auth_url() if args.auth_url is None else args.auth_url
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(args.repo, auth_url), encoding="utf-8")


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    main()
