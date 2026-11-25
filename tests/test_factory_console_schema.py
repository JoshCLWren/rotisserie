"""`baste schema`: the store schema status, --apply, doctor's warning and both control rooms."""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rotisserie import cli
from rotisserie import control_contract as contract
from rotisserie.control_parity import CONTROLS
from rotisserie.factory_console import commands
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import bridge, schema
from rotisserie.hosted_operator import execute

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
INDEX = "idx_factory_backoff_repo_kind"
STATUS: dict[str, Any] = {
    "status": "missing",
    "reasons": [f"1 expected object is missing: {INDEX}"],
    "code_version": 2,
    "stored_version": 2,
    "objects": [
        {"kind": "table", "name": "factory_backoff", "present": True},
        {"kind": "index", "name": INDEX, "present": False},
    ],
    "missing": [INDEX],
    "last_upgrade": {
        "result": "failed",
        "error": "InsufficientPrivilegeError: CREATE INDEX denied",
        "created": None,
        "source": "runtime",
        "workflow": "Factory Dispatch",
        "run_id": "4242",
        "version": 2,
        "at": "2026-11-24T10:00:00+00:00",
    },
    "checked_at": NOW.isoformat(),
}


class Out(io.StringIO):
    def isatty(self) -> bool:
        return False


def _run(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, calls: list[Any] | None = None
) -> tuple[int, Any, str]:
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None, allow_exit_one=False: "[]")

    def fake_run(github, clock, workflow, inputs, **kwargs):  # type: ignore[no-untyped-def]
        assert calls is not None
        calls.append((workflow, inputs, kwargs["artifact"]))
        if workflow == schema.READ_WORKFLOW:
            envelope = execute(
                "read", inputs["request"], "acme/widgets", lambda repo, args: {**STATUS}
            )
            data: dict[str, object] = envelope
        else:
            data = {**STATUS, "status": "ok", "missing": [], "applied": "ok"}
        return bridge.BridgeRun(workflow, 7, "https://example.invalid/run/7", "success", data)

    monkeypatch.setattr(bridge, "run", fake_run)
    as_json = "--json" in argv
    argv = [item for item in argv if item != "--json"]
    arguments = cli.parser().parse_args(
        ["factory", "--repo", "acme/widgets", *(["--json"] if as_json else []), *argv]
    )
    out = Out()
    code = commands.main(arguments, env={"TERM": "xterm"}, stdout=out, stdin=Out(), executable="sh")
    text = out.getvalue()
    return code, json.loads(text) if as_json else None, text


def test_via_github_reads_through_the_read_only_operator_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    code, data, _ = _run(["schema", "--via", "github", "--json"], monkeypatch, calls)
    assert code == 0
    ((workflow, inputs, artifact),) = calls
    assert workflow == "rotisserie-operator-read.yml"
    assert artifact == "rotisserie-operator-{run_id}"
    assert json.loads(inputs["request"]) == {"operation": "read", "reads": [{"name": "schema"}]}
    assert data["missing"] == [INDEX] and data["bridge"]["run_id"] == 7
    assert data["last_upgrade"]["result"] == "failed"


def test_the_text_output_names_each_object_and_the_last_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, _, text = _run(["schema", "--via", "github"], monkeypatch, [])
    assert code == 0
    assert "Store schema: missing" in text and "Code version 2, stored version 2" in text
    assert "Last upgrade: failed" in text and "Factory Dispatch run 4242" in text
    assert "CREATE INDEX denied" in text
    assert INDEX in text and "MISSING" in text and "present" in text


def test_apply_runs_through_the_label_claim_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    code, data, _ = _run(["schema", "--apply", "--via", "github", "--json"], monkeypatch, calls)
    assert code == 0
    assert calls == [
        ("rotisserie-label-claim.yml", {"mode": "schema-apply"}, schema.APPLY_ARTIFACT)
    ]
    assert data["applied"] == "ok"


def _sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *statements: str) -> Path:
    path = tmp_path / "store.db"
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(path))
    assert schema.apply_direct()["status"] == "ok"
    conn = sqlite3.connect(path)
    for sql in statements:
        conn.execute(sql)
    conn.commit()
    conn.close()
    return path


def test_direct_status_and_apply_against_a_sqlite_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sqlite(tmp_path, monkeypatch, f"DROP INDEX {INDEX}")
    code, data, _ = _run(["schema", "--json"], monkeypatch)
    assert code == 0 and data["status"] == "missing" and data["missing"] == [INDEX]
    code, data, _ = _run(["schema", "--apply", "--json"], monkeypatch)
    assert code == 0 and data["applied"] == "ok" and data["status"] == "ok"
    assert data["last_upgrade"]["created"] == [INDEX]


def test_direct_status_reports_a_version_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sqlite(tmp_path, monkeypatch, "DELETE FROM _schema_version WHERE version > 1")
    code, data, _ = _run(["schema", "--json"], monkeypatch)
    assert code == 0 and data["status"] == "behind" and data["stored_version"] == 1
    assert data["missing"] == []


def test_without_a_store_the_direct_read_points_at_the_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    code, data, _ = _run(["schema", "--json"], monkeypatch)
    assert code != 0 and "--via github" in json.dumps(data)


def test_doctor_warns_on_missing_objects_a_version_behind_and_a_stale_status() -> None:
    status, text, hint = schema.health({"schema": STATUS}, NOW)
    assert status == "warn" and INDEX in text and "--apply" in hint
    behind = {
        **STATUS,
        "status": "behind",
        "reasons": ["stored schema version 1 is behind this code's 2"],
    }
    status, text, _ = schema.health({"schema": behind}, NOW)
    assert status == "warn" and "behind" in text
    fine = {**STATUS, "status": "ok", "reasons": []}
    assert schema.health({"schema": fine}, NOW)[0] == "ok"
    status, text, _ = schema.health({"schema": fine}, NOW + timedelta(hours=2))
    assert status == "warn" and "stale" in text
    assert schema.health(None, NOW)[0] == "ok"
    assert schema.health({"schema": {"status": "not_configured"}}, NOW)[0] == "ok"


def test_doctor_runs_the_schema_check() -> None:
    source = (ROOT / "src/rotisserie/factory_console/service/doctor.py").read_text()
    assert 'Finding("store schema", *schema.health(stats(), now))' in source


def test_both_control_rooms_read_and_apply_the_schema() -> None:
    assert contract.read_command("schema", {}) == ["schema", "--via", "github"]
    assert contract.read_command("schema", {}, trusted=True) == ["schema"]
    assert "schema" in contract.LIVE_READS
    assert contract.write_command({"operation": "schema_apply"}) == [
        "schema",
        "--apply",
        "--via",
        "github",
    ]
    assert CONTROLS["schema"] == ("read:schema", "write:schema_apply")
    ui = "".join(
        (ROOT / "src/rotisserie" / name).read_text() for name in ("web_ui.html", "web_ui.js")
    )
    assert 'get("schema")' in ui and 'operation:"schema_apply"' in ui
    assert 'id="schema-read"' in ui and 'id="schema-apply"' in ui


def test_workflows_publish_the_status_and_offer_the_apply_mode() -> None:
    pages = (ROOT / ".github/workflows/factory-status-pages.yml").read_text(encoding="utf-8")
    assert "python3 .github/scripts/factory_turso_schema.py status" in pages
    assert ".schema = $schema[0]" in pages
    claim = (ROOT / ".github/workflows/rotisserie-label-claim.yml").read_text(encoding="utf-8")
    assert "schema-apply) args=(factory schema --apply) ;;" in claim
    caller = (ROOT / "factory/install/callers/rotisserie-label-claim.yml").read_text()
    assert "schema-apply]" in caller
    overlay = (ROOT / "factory/install/overlay-paths.txt").read_text().split()
    assert ".github/scripts/factory_turso_schema.py" in overlay
