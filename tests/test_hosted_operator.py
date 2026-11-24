"""Hosted operator requests: one shared contract, a result file for every outcome."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from rotisserie import control_contract as contract
from rotisserie import hosted_operator, web

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CALLERS = ROOT / "factory" / "install" / "callers"
REQUEST_ID = "0f6c5e2a-4b1d-4c55-9b0e-123456789abc"


class FakeCli:
    def __init__(self, answers: dict[str, dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.answers = answers or {}

    def __call__(self, repository: str, args: list[str]) -> dict[str, Any]:
        self.calls.append((repository, args))
        return dict(self.answers.get(args[0], {"result": "completed", "exit_code": 0}))


def test_local_web_ui_and_operator_share_one_contract() -> None:
    assert web.read_command is contract.read_command
    assert web.write_command is contract.write_command
    assert web.InvalidRequest is contract.InvalidRequest
    assert contract.PUBLIC_READS <= contract.READ_ONLY
    assert contract.LIVE_READS == contract.READ_ONLY - contract.PUBLIC_READS


def test_write_operations_table_matches_the_write_builder() -> None:
    with pytest.raises(contract.InvalidRequest, match="Unknown write operation"):
        contract.write_command({"operation": "shell"})
    for name in contract.WRITE_OPERATIONS:
        try:
            contract.write_command({"operation": name})
        except contract.InvalidRequest as exc:
            assert "Unknown write operation" not in str(exc), name


@pytest.mark.parametrize("value", ["short", "a" * 65, "bad id here", "../../x", 7, True])
def test_request_id_is_bounded(value: object) -> None:
    with pytest.raises(contract.InvalidRequest):
        contract.request_id({"request_id": value})


def test_request_id_is_optional_for_older_callers() -> None:
    assert contract.request_id({}) == ""
    assert contract.request_id({"request_id": REQUEST_ID}) == REQUEST_ID


def test_read_batch_validates_every_read() -> None:
    body = {
        "operation": "read",
        "reads": [
            {"name": "why", "query": {}},
            {"name": "models", "query": {"provider": "opencode-go"}},
            {"name": "unstick"},
        ],
    }
    assert [args for _, _, args in contract.read_batch(body)] == [
        ["why"],
        ["models", "opencode-go"],
        ["unstick", "--dry-run"],
    ]


@pytest.mark.parametrize(
    "reads",
    [
        [],
        [{"name": "why"}] * (contract.MAX_BATCH + 1),
        [{"name": "run"}],
        [{"name": "models", "query": {"provider": "--x"}}],
        [{"name": "why", "query": {"target": 1}}],
        ["why"],
        None,
    ],
)
def test_read_batch_rejects_writes_and_bad_shapes(reads: object) -> None:
    with pytest.raises(contract.InvalidRequest):
        contract.read_batch({"operation": "read", "reads": reads})


def test_read_mode_runs_each_read_and_keeps_the_request_id() -> None:
    cli = FakeCli({"why": {"issues": [], "exit_code": 1}})
    raw = json.dumps(
        {
            "operation": "read",
            "request_id": REQUEST_ID,
            "reads": [{"name": "why"}, {"name": "leases"}],
        }
    )
    envelope = hosted_operator.execute("read", raw, "acme/widgets", cli)
    assert cli.calls == [("acme/widgets", ["why"]), ("acme/widgets", ["leases"])]
    assert envelope["request_id"] == REQUEST_ID
    assert envelope["kind"] == "read"
    assert [item["name"] for item in envelope["reads"]] == ["why", "leases"]
    assert envelope["reads"][0]["result"] == {"issues": [], "exit_code": 1}
    assert envelope["exit_code"] == 1
    assert envelope["finished_at"]


def test_read_mode_never_runs_a_write() -> None:
    cli = FakeCli()
    raw = json.dumps({"operation": "unstick", "request_id": REQUEST_ID})
    envelope = hosted_operator.execute("read", raw, "acme/widgets", cli)
    assert cli.calls == []
    assert envelope["exit_code"] == hosted_operator.EXIT_REJECTED
    assert envelope["error"].startswith("Rejected factory command")
    assert envelope["request_id"] == REQUEST_ID


def test_write_mode_refuses_reads_so_each_workflow_keeps_its_role() -> None:
    cli = FakeCli()
    raw = json.dumps({"operation": "read", "reads": [{"name": "why"}]})
    envelope = hosted_operator.execute("write", raw, "acme/widgets", cli)
    assert cli.calls == []
    assert "read-only operator workflow" in envelope["error"]


def test_write_mode_reports_the_cli_failure_text() -> None:
    failure = {"result": "rejected", "error": "Invalid provider/model identifier", "exit_code": 2}
    cli = FakeCli({"run": failure})
    raw = json.dumps(
        {
            "operation": "run",
            "provider": "opencode-go",
            "model": "hy4-flash-free",
            "target": "pr:5671",
            "request_id": REQUEST_ID,
        }
    )
    envelope = hosted_operator.execute("write", raw, "acme/widgets", cli)
    assert cli.calls[0][1][:3] == ["run", "opencode-go", "hy4-flash-free"]
    assert envelope["operation"] == "run"
    assert envelope["result"]["error"] == "Invalid provider/model identifier"
    assert envelope["exit_code"] == 2


@pytest.mark.parametrize(
    "raw",
    ["not json", "[1]", json.dumps({"operation": "run", "provider": "x; rm -rf /"}), "x" * 5000],
)
def test_rejected_requests_never_reach_the_cli(raw: str) -> None:
    cli = FakeCli()
    envelope = hosted_operator.execute("write", raw, "acme/widgets", cli)
    assert cli.calls == []
    assert envelope["exit_code"] == hosted_operator.EXIT_REJECTED


def test_main_writes_the_result_file_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    monkeypatch.setenv("ROTISSERIE_REQUEST", json.dumps({"operation": "claim", "target": "pr:4"}))
    out = tmp_path / "result.json"
    cli = FakeCli({"claim": {"status": "rejected", "exit_code": 3}})
    assert hosted_operator.main(["write", "--output", str(out)], runner=cli) == 3
    assert json.loads(out.read_text())["result"]["status"] == "rejected"

    monkeypatch.setenv(
        "ROTISSERIE_REQUEST", json.dumps({"operation": "read", "reads": [{"name": "why"}]})
    )
    cli = FakeCli({"why": {"exit_code": 1}})
    assert hosted_operator.main(["read", "--output", str(out)], runner=cli) == 0
    assert json.loads(out.read_text())["reads"][0]["command"] == ["why"]

    monkeypatch.setenv("ROTISSERIE_REQUEST", "{}")
    assert hosted_operator.main(["read", "--output", str(out)], runner=cli) == 2


def test_read_workflow_is_read_only_and_admin_gated() -> None:
    source = (WORKFLOWS / "rotisserie-operator-read.yml").read_text(encoding="utf-8")
    for scope in ("contents", "issues", "pull-requests", "actions"):
        assert f"{scope}: read" in source
        assert f"{scope}: write" not in source
    assert source.index("Authorize GitHub actor") < source.index("Check out adopter main")
    assert "ROTISSERIE_ADMIN_GITHUB_IDS" in source
    assert "uses: ./.github/actions/factory-store" in source
    assert "token: ${{ secrets.FACTORY_TURSO_READ_TOKEN }}" in source
    assert "rotisserie.hosted_operator read --output" in source
    assert "name: rotisserie-operator-${{ github.run_id }}" in source
    assert "retention-days: 1" in source
    assert "if: always()" in source


def test_write_workflow_publishes_its_result_even_on_failure() -> None:
    source = (WORKFLOWS / "rotisserie-operator.yml").read_text(encoding="utf-8")
    assert "rotisserie.hosted_operator write --output" in source
    assert "name: rotisserie-operator-${{ github.run_id }}" in source
    publish = source[source.index("Publish the command result") :]
    assert "if: always()" in publish
    assert "retention-days: 1" in publish
    assert "inputs:\n      request:" in source
    assert "request_id" not in source


def test_read_caller_passes_only_the_request_and_read_only_store_secrets() -> None:
    source = (CALLERS / "rotisserie-operator-read.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in source
    assert "rotisserie-operator-read.yml@main" in source
    assert "FACTORY_TURSO_READ_TOKEN: ${{ secrets.FACTORY_TURSO_READ_TOKEN }}" in source
    assert "FACTORY_TURSO_TOKEN:" not in source and "NEON" not in source
    assert ": write" not in source


def test_close_needs_a_pull_request_and_a_reason() -> None:
    body = {"operation": "close", "target": "pr:4", "reason": "duplicate of #3"}
    assert contract.write_command(body) == ["close", "pr:4", "--reason=duplicate of #3", "--yes"]
    keep = contract.write_command({**body, "keep_issue": True})
    assert keep[-1] == "--keep-issue"
    for bad in ({"reason": ""}, {"reason": "  "}, {"target": "issue:4"}, {"reason": "a|b"}):
        with pytest.raises(contract.InvalidRequest):
            contract.write_command({**body, **bad})


def test_limits_is_a_live_read_without_arguments() -> None:
    assert contract.read_command("limits", {}) == ["limits"]
    assert "limits" in contract.LIVE_READS


def test_backoffs_read_uses_neon_only_inside_the_read_workflow() -> None:
    assert contract.read_command("backoffs", {}) == ["backoffs", "--via", "github"]
    assert contract.read_command("backoffs", {}, trusted=True) == ["backoffs"]
    cli = FakeCli()
    raw = json.dumps({"operation": "read", "reads": [{"name": "backoffs"}]})
    hosted_operator.execute("read", raw, "acme/widgets", cli)
    assert cli.calls == [("acme/widgets", ["backoffs"])]
    with pytest.raises(contract.InvalidRequest):
        contract.read_command("backoffs", {"action": "reset"})


def test_backoffs_reset_is_quota_only_through_the_trusted_workflow() -> None:
    assert contract.write_command({"operation": "backoffs_reset"}) == [
        "backoffs",
        "reset",
        "--quota-only",
        "--yes",
        "--via",
        "github",
    ]


def test_bridged_commands_get_the_bridge_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    def fake_run(command: list[str], **options: Any) -> Any:
        seen.append(options["timeout"])
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    web.invoke("acme/widgets", ["backoffs", "--via", "github"])
    web.invoke("acme/widgets", ["labels", "--via", "github", "--no-wait"])
    web.invoke("acme/widgets", ["why"])
    assert seen == [900, 90, 90]
