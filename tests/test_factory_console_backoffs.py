"""`baste backoffs`: read and quota-only reset through trusted workflows."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from rotisserie import cli
from rotisserie.factory_console import commands
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import backoffs, bridge

LISTING: dict[str, object] = {
    "schema_version": 1,
    "providers": {
        "openrouter-free": {
            "capacity": [
                {
                    "scope": "account",
                    "kind": "daily",
                    "model": "x:free",
                    "reset_source": "key_endpoint",
                    "until": "2026-11-25T00:00:00+00:00",
                }
            ],
            "fibonacci": [
                {
                    "kind": "model",
                    "model": "y:free",
                    "streak": 16,
                    "quota_caused": True,
                    "until": "2026-11-25T00:00:00+00:00",
                }
            ],
            "health": [],
        }
    },
}


class Out(io.StringIO):
    def isatty(self) -> bool:
        return False


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch, calls: list[Any]) -> tuple[int, Any]:
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None, allow_exit_one=False: "[]")

    def fake_run(github, clock, workflow, inputs, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((workflow, inputs, kwargs["artifact"]))
        data: dict[str, object] = (
            LISTING if workflow == backoffs.LABEL_WORKFLOW else {"quota_backoff_rows": 3}
        )
        return bridge.BridgeRun(workflow, 7, "https://example.invalid/run/7", "success", data)

    monkeypatch.setattr(bridge, "run", fake_run)
    arguments = cli.parser().parse_args(["factory", "--repo", "acme/widgets", "--json", *argv])
    out = Out()
    code = commands.main(arguments, env={"TERM": "xterm"}, stdout=out, stdin=Out(), executable="sh")
    return code, json.loads(out.getvalue())


def test_backoffs_list_reads_through_the_label_claim_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    code, data = _run(["backoffs", "--via", "github"], monkeypatch, calls)
    assert code == 0
    assert calls == [("rotisserie-label-claim.yml", {"mode": "backoffs"}, backoffs.LABEL_ARTIFACT)]
    capacity = data["providers"]["openrouter-free"]["capacity"][0]
    assert capacity["reset_source"] == "key_endpoint"
    assert data["bridge"]["run_id"] == 7


def test_quota_only_reset_runs_the_trusted_reset_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    code, data = _run(
        ["backoffs", "reset", "--quota-only", "--yes", "--via", "github"], monkeypatch, calls
    )
    assert code == 0
    assert calls == [
        ("factory-model-health-reset.yml", {"quota_only": "true"}, backoffs.RESET_ARTIFACT)
    ]
    assert data["quota_backoff_rows"] == 3


def test_full_reset_is_not_offered_and_direct_read_needs_neon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FACTORY_TURSO_TOKEN_FILE", raising=False)
    calls: list[Any] = []
    for argv in (["backoffs", "reset", "--yes"], ["backoffs"]):
        code, data = _run(argv, monkeypatch, calls)
        assert code != 0
    assert calls == []
    with pytest.raises(backoffs.BackoffsUnavailableError, match="--via github"):
        backoffs.read_direct("acme/widgets")
