"""`run --exact` asks for exact-or-nothing; without it a pinned model may fall back."""

from __future__ import annotations

from pathlib import Path

import pytest

from rotisserie import cli, web
from rotisserie.factory_console import runtime
from rotisserie.factory_console.service import dispatch

MODEL = "nvidia/meta/muse-glimmer-30b"
UI = Path(__file__).resolve().parents[1] / "src" / "rotisserie"


def test_cli_accepts_exact() -> None:
    parsed = cli.parser().parse_args(["factory", "run", "nvidia", MODEL, "--exact"])
    assert parsed.exact is True
    assert cli.parser().parse_args(["factory", "run", "nvidia", MODEL]).exact is False


def test_exact_request_prefixes_the_dispatched_model() -> None:
    provider = runtime.resolve_provider("nvidia")
    exact = dispatch.Request(provider, MODEL, exact=True)
    assert exact.inputs()["model"] == f"exact:{MODEL}"
    assert exact.to_dict()["exact"] is True
    assert dispatch.Request(provider, MODEL).inputs()["model"] == MODEL


def test_control_rooms_map_exact_to_the_cli_flag() -> None:
    body = {"operation": "run", "provider": "nvidia", "model": MODEL, "exact": True}
    assert web.write_command(body) == ["run", "nvidia", MODEL, "--no-wait", "--yes", "--exact"]
    with pytest.raises(web.InvalidRequest):
        web.write_command({**body, "model": "auto"})
    page = (UI / "web_ui.html").read_text(encoding="utf-8")
    script = (UI / "web_ui.js").read_text(encoding="utf-8")
    assert 'id="exact"' in page
    assert 'exact:el("exact").checked' in script
    assert 'args.push("--exact")' in script
