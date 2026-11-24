#!/usr/bin/env python3
"""Publish the existing local Rotisserie control-room UI on GitHub Pages.

The generated HTML is deliberately a static client. Public reads come only
from sanitized status snapshots; operator writes go through an authenticated
GitHub Actions dispatch, not directly to privileged factory credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "src" / "rotisserie" / "web_ui.html"
ADAPTER = Path(__file__).with_name("hosted_control_room.js")
REQUEST = re.compile(r"(?m)^async function request\(path,body\)\{[^\n]*\}$")
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


def render(repo: str, auth_url: str = "") -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
        raise ValueError("Invalid repository name")
    auth_url = auth_url.rstrip("/")
    if auth_url and not AUTH_URL.fullmatch(auth_url):
        raise ValueError("Dashboard sign-in URL must be a plain https URL")
    auth_origin = "/".join(auth_url.split("/")[:3]) if auth_url else ""
    page = UI.read_text(encoding="utf-8")
    page = page.replace("__SESSION_TOKEN__", "null").replace("__REPOSITORY__", json.dumps(repo))
    page, count = REQUEST.subn(
        "async function request(path,body){return window.hostedRequest(path,body)}", page
    )
    if count != 1:
        raise RuntimeError("Local control-room request contract changed; refusing to publish")
    page = page.replace(
        '<script>\n"use strict";',
        "<script>\n"
        + ADAPTER.read_text(encoding="utf-8").replace('"__AUTH_BASE__"', json.dumps(auth_url))
        + "\n</script>\n<script>\n\"use strict\";",
        1,
    )
    page = page.replace("Local operator session", "Hosted factory dashboard")
    page = page.replace(
        "GitHub via your existing gh login",
        "GitHub authorizes remote operations. Read-only status is public.",
    )
    page = page.replace(
        "Served locally · All mutations pass through existing factory commands",
        "GitHub Pages · Published snapshot · Operator writes use GitHub Actions",
    )
    page = page.replace(
        "</aside>",
        '<div class="sidebar-bottom" id="hosted-admin">'
        '<strong>GitHub operator access</strong>'
        '<p class="hint">Use your existing GitHub account. '
        'Only approved administrators can control the factory.</p>'
        '<button class="button small" id="github-connect">Sign in with GitHub</button>'
        '<div class="hint" id="github-identity">Read-only visitor</div>'
        '</div></aside>',
        1,
    )
    page = page.replace(
        "</head>",
        '<meta name="referrer" content="no-referrer">'
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src &#39;none&#39;; script-src &#39;unsafe-inline&#39;; '
        'style-src &#39;unsafe-inline&#39;; connect-src https://raw.githubusercontent.com'
        + (f" {auth_origin}" if auth_origin else "")
        + '; img-src data:; base-uri &#39;none&#39;; '
        'form-action &#39;none&#39;; frame-ancestors &#39;none&#39;">'
        "</head>",
        1,
    )
    return page


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
    main()
