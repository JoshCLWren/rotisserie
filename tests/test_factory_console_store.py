"""`rotisserie factory store`: self-test and backup export through trusted workflows."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from rotisserie import cli
from rotisserie import control_contract as contract
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


def _encrypt(plain: bytes, folder: Path, key: str) -> Path:
    source = folder / "plain.sqlite"
    source.write_bytes(plain)
    encrypted = folder / "factory-state.sqlite.enc"
    subprocess.run(
        ["openssl", "enc", *store.CIPHER, "-salt", "-pass", "env:FACTORY_EXPORT_KEY"]
        + ["-in", str(source), "-out", str(encrypted)],
        env={**os.environ, "FACTORY_EXPORT_KEY": key},
        check=True,
    )
    return encrypted


def test_decrypt_restores_the_export_readable_only_by_its_owner(tmp_path: Path) -> None:
    encrypted = _encrypt(b"SQLite format 3\x00rows", tmp_path, "s3cret")
    out = tmp_path / "backup.sqlite"
    store.decrypt(encrypted, out, "s3cret")
    assert out.read_bytes() == b"SQLite format 3\x00rows"
    assert out.stat().st_mode & 0o777 == 0o600


def test_decrypt_with_the_wrong_key_leaves_no_file(tmp_path: Path) -> None:
    encrypted = _encrypt(b"SQLite format 3\x00rows", tmp_path, "s3cret")
    out = tmp_path / "backup.sqlite"
    with pytest.raises(bridge.BridgeError, match="FACTORY_EXPORT_KEY"):
        store.decrypt(encrypted, out, "wrong")
    assert not out.exists()


def test_download_needs_the_export_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FACTORY_EXPORT_KEY", raising=False)
    with pytest.raises(bridge.BridgeError, match="FACTORY_EXPORT_KEY"):
        store.download(GitHub("acme/widgets"), 1, tmp_path / "backup.sqlite")


def test_export_workflow_uploads_only_the_encrypted_file() -> None:
    workflow = Path(".github/workflows/factory-state-export.yml").read_text()
    assert "path: ${{ runner.temp }}/export/factory-state.sqlite.enc" in workflow
    assert 'rm -f "$RUNNER_TEMP/export/factory-state.sqlite"' in workflow
    assert " ".join(store.CIPHER) in workflow


MIGRATED: dict[str, object] = {
    "status": "ok",
    "dry_run": True,
    "neon_tables": {"factory_state_kv": 12, "factory_state_events": 900},
    "absent_tables": ["factory_check_projection"],
    "checks": {
        "projection_keys": 40,
        "provider_budget_set": [{"source": "nvidia", "columns": ["concurrency_limit"]}],
    },
    "migration": {
        "kv:factory-lease-v1": {
            "target": "factory_lease",
            "source": 3,
            "eligible": 2,
            "inserted": 0,
            "present": 0,
            "skipped": {"claim_id_missing": 1},
        }
    },
    "mismatches": [],
}


@pytest.mark.parametrize(("flags", "dry_run"), [(["--dry-run"], "true"), ([], "false")])
def test_migrate_always_names_dry_run(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], dry_run: str
) -> None:
    calls: list[Any] = []
    code, data = _run(["store", "migrate", *flags], monkeypatch, calls, MIGRATED)
    assert code == 0 and data["checks"]["projection_keys"] == 40
    assert calls == [
        ("factory-store-migrate.yml", {"dry_run": dry_run}, "factory-store-migrate-{run_id}")
    ]


def test_migrate_without_wait_starts_the_run_with_the_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[Any] = []

    def fake_start(github: GitHub, workflow: str, inputs: dict[str, str]) -> tuple[int, str]:
        started.append((workflow, inputs))
        return 5, "https://example.invalid/run/5"

    monkeypatch.setattr(bridge, "_start", fake_start)
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None, allow_exit_one=False: "[]")
    arguments = cli.parser().parse_args(
        ["factory", "--repo", "acme/widgets", "--json", "store", "migrate", "--no-wait"]
    )
    out = Out()
    code = commands.main(arguments, env={"TERM": "xterm"}, stdout=out, stdin=Out(), executable="sh")
    assert code == 0 and json.loads(out.getvalue())["bridge"]["run_id"] == 5
    assert started == [("factory-store-migrate.yml", {"dry_run": "false"})]
    assert store.start(GitHub("acme/widgets"), "migrate")["status"] == "started"
    assert started[-1] == ("factory-store-migrate.yml", {"dry_run": "true"})


def test_a_mismatched_migration_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = {**MIGRATED, "status": "mismatch", "mismatches": [{"group": "x", "check": "y"}]}
    code, data = _run(["store", "migrate"], monkeypatch, [], summary)
    assert code != 0 and data["status"] == "mismatch"


def test_migrate_prints_the_checks_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None, allow_exit_one=False: "[]")
    monkeypatch.setattr(
        bridge,
        "run",
        lambda *a, **k: bridge.BridgeRun(
            "w", 9, "https://example.invalid/run/9", "success", MIGRATED
        ),
    )
    arguments = cli.parser().parse_args(
        ["factory", "--repo", "acme/widgets", "store", "migrate", "--dry-run"]
    )
    out = Out()
    code = commands.main(arguments, env={"TERM": "xterm"}, stdout=out, stdin=Out(), executable="sh")
    text = out.getvalue()
    assert code == 0
    assert "Store dry run: ok" in text and "Projection keys in Neon: 40" in text
    assert "Budget row nvidia: concurrency_limit" in text
    assert "Neon factory_check_projection: absent" in text
    assert "kv:factory-lease-v1: source 3, eligible 2" in text and "claim_id_missing 1" in text


def test_dry_run_works_with_migrate_only(monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = cli.parser().parse_args(
        ["factory", "--repo", "acme/widgets", "store", "selftest", "--dry-run"]
    )
    code = commands.main(
        arguments, env={"TERM": "xterm"}, stdout=Out(), stdin=Out(), executable="sh"
    )
    assert code == EXIT_USAGE


def test_the_contract_runs_migrate_as_a_dry_run_unless_told_otherwise() -> None:
    assert contract.write_command({"operation": "store_migrate"}) == [
        "store",
        "migrate",
        "--no-wait",
        "--dry-run",
    ]
    assert contract.write_command({"operation": "store_migrate", "dry_run": False}) == [
        "store",
        "migrate",
        "--no-wait",
    ]
    with pytest.raises(contract.InvalidRequest):
        contract.write_command({"operation": "store_migrate", "dry_run": "false"})
    assert contract.read_command("store", {"mode": "migrate", "run": "7"}) == [
        "store",
        "migrate",
        "--run",
        "7",
    ]
    with pytest.raises(contract.InvalidRequest):
        contract.read_command("store", {"mode": "drop", "run": "7"})


def test_the_web_ui_confirms_a_real_migration_and_picks_the_run_kind() -> None:
    script = Path("src/rotisserie/web_ui.js").read_text()
    page = Path("src/rotisserie/web_ui.html").read_text()
    assert 'act({operation:"store_migrate",dry_run:true}' in script
    real = script.split('el("store-migrate").onclick=')[1].split("\n")[0]
    assert real.index("window.confirm(") < real.index("dry_run:false")
    assert 'const mode=el("store-mode").value' in script
    assert '<option value="migrate">' in page and 'id="store-migrate-dry"' in page
