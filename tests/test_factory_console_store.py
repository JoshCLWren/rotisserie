"""`rotisserie factory store`: self-test and backup export through trusted workflows."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from rotisserie import cli
from rotisserie.factory_console import commands
from rotisserie.factory_console.commands._shared import EXIT_USAGE
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import bridge, store


class Out(io.StringIO):
    def isatty(self) -> bool:
        return False


def _run(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, calls: list[Any], data: dict[str, object]
) -> tuple[int, Any]:
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None, allow_exit_one=False: "[]")

    def fake_run(github, clock, workflow, inputs, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((workflow, inputs, kwargs["artifact"]))
        return bridge.BridgeRun(workflow, 9, "https://example.invalid/run/9", "success", data)

    monkeypatch.setattr(bridge, "run", fake_run)
    arguments = cli.parser().parse_args(["factory", "--repo", "acme/widgets", "--json", *argv])
    out = Out()
    code = commands.main(arguments, env={"TERM": "xterm"}, stdout=out, stdin=Out(), executable="sh")
    return code, json.loads(out.getvalue())


def test_selftest_runs_the_trusted_workflow_and_reports_its_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    summary = {"status": "passed", "passed": 40, "failed": 0, "skipped": 9}
    code, data = _run(["store", "selftest", "--measure"], monkeypatch, calls, summary)
    assert code == 0
    assert calls == [
        ("factory-turso-selftest.yml", {"measure": "true"}, "factory-turso-selftest-{run_id}")
    ]
    assert data["passed"] == 40 and data["bridge"]["run_id"] == 9


def test_a_failed_selftest_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    code, data = _run(["store", "selftest"], monkeypatch, [], {"status": "failed", "failed": 2})
    assert code != 0 and data["failed"] == 2


def test_export_downloads_the_sqlite_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    saved: list[Any] = []

    def fake_download(github: GitHub, run_id: int, destination: Path) -> Path:
        saved.append(run_id)
        destination.write_bytes(b"SQLite format 3\x00")
        return destination

    monkeypatch.setattr(store, "download", fake_download)
    out = tmp_path / "backup.sqlite"
    summary: dict[str, object] = {"status": "ok", "rows": {"factory_lease": 2}}
    code, data = _run(["store", "export", "--out", str(out)], monkeypatch, [], summary)
    assert code == 0 and saved == [9] and data["file"] == str(out)
    assert out.read_bytes().startswith(b"SQLite format 3")


def test_out_needs_export_and_a_finished_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    arguments = cli.parser().parse_args(
        ["factory", "--repo", "acme/widgets", "store", "selftest", "--out", "x.sqlite"]
    )
    monkeypatch.setattr(bridge, "run", lambda *a, **k: calls.append(a))
    code = commands.main(
        arguments, env={"TERM": "xterm"}, stdout=Out(), stdin=Out(), executable="sh"
    )
    assert code == EXIT_USAGE and calls == []


def test_store_download_refuses_to_overwrite(tmp_path: Path) -> None:
    existing = tmp_path / "keep.sqlite"
    existing.write_text("keep")
    with pytest.raises(FileExistsError):
        store.download(GitHub("acme/widgets"), 1, existing)
