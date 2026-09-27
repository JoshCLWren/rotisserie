from __future__ import annotations

import json
import shutil
from pathlib import Path

from rotisserie.cli import EXIT_INVALID, EXIT_OK, main
from rotisserie.operator import OperationJournal, load_config, redact

EXAMPLE = Path(__file__).parents[1] / "examples" / "local"


def local_example(tmp_path: Path, *, mutations: bool = True) -> Path:
    shutil.copy(EXAMPLE / "graph.json", tmp_path / "graph.json")
    config = tmp_path / "config.toml"
    config.write_text(
        f"""schema_version = 1
[repository]
host = "example.test"
owner = "rotisserie"
name = "demo"
[repositories]
allow = [{{ host = "example.test", owner = "rotisserie", name = "demo" }}]
[local]
snapshot = "graph.json"
state_directory = "state"
mutations_enabled = {str(mutations).lower()}
""",
        encoding="utf-8",
    )
    return config


def invoke(capsys: object, config: Path, *arguments: str) -> tuple[int, dict[str, object]]:
    code = main(["--config", str(config), "--correlation-id", "test-operation", *arguments])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    value = json.loads(captured.out)
    assert isinstance(value, dict)
    return code, value


def test_config_paths_are_relative_and_repository_is_allowlisted(tmp_path: Path) -> None:
    config = load_config(local_example(tmp_path))
    assert config.snapshot == tmp_path / "graph.json"
    assert config.state_directory == tmp_path / "state"
    assert config.repository in config.allowed_repositories


def test_clean_clone_fixture_can_inspect_and_plan(capsys: object, tmp_path: Path) -> None:
    config = local_example(tmp_path)
    code, output = invoke(capsys, config, "inspect")
    assert code == EXIT_OK
    assert output["graph"] == {
        "schema_version": 1,
        "works": 2,
        "changes": 1,
        "leases": 0,
        "checks": 1,
        "reviews": 1,
    }

    code, output = invoke(capsys, config, "plan", "--at", "10", "--max-active", "2")
    assert code == EXIT_OK
    assert output["selected"] == ["1"]
    assert output["ready_changes"] == ["20"]


def test_mutations_are_dry_run_by_default_and_apply_requires_config(
    capsys: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path, mutations=False)
    arguments = (
        "claim",
        "1",
        "human:producer",
        "--lease-id",
        "lease-1",
        "--at",
        "10",
        "--expires-at",
        "20",
    )
    code, output = invoke(capsys, config, *arguments)
    assert code == EXIT_OK
    assert output["status"] == "planned"
    state = json.loads((tmp_path / "state" / "graph-state.json").read_text())
    assert state["snapshot"]["leases"] == []

    code, output = invoke(capsys, config, *arguments, "--apply")
    assert code == EXIT_INVALID
    assert output["status"] == "invalid"
    state = json.loads((tmp_path / "state" / "graph-state.json").read_text())
    assert state["snapshot"]["leases"] == []


def test_local_claim_run_recover_review_and_complete_lifecycle(
    capsys: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path)
    code, claim = invoke(
        capsys,
        config,
        "claim",
        "1",
        "human:producer",
        "--lease-id",
        "lease-1",
        "--at",
        "10",
        "--expires-at",
        "20",
        "--apply",
    )
    assert code == EXIT_OK
    assert claim["status"] == "applied"

    code, run = invoke(capsys, config, "run", "1", "human:producer", "--at", "11", "--apply")
    assert code == EXIT_OK
    assert run["status"] == "applied"

    code, review = invoke(
        capsys,
        config,
        "review",
        "20",
        "demo-head",
        "human:reviewer",
        "--apply",
    )
    assert code == EXIT_OK
    assert review["status"] == "applied"

    code, complete = invoke(capsys, config, "complete", "20", "demo-head", "--apply")
    assert code == EXIT_OK
    assert complete["status"] == "applied"

    code, recover = invoke(capsys, config, "recover", "--at", "20", "--apply")
    assert code == EXIT_OK
    assert recover["status"] == "applied"
    state = json.loads((tmp_path / "state" / "graph-state.json").read_text())
    works = {item["id"]["key"]: item for item in state["snapshot"]["works"]}
    assert works["2"]["state"] == "completed"
    assert state["snapshot"]["leases"] == []


def test_logs_metrics_and_diagnostic_bundle_are_redacted(capsys: object, tmp_path: Path) -> None:
    config = local_example(tmp_path)
    journal = OperationJournal(tmp_path / "state")
    journal.append(
        correlation_id="correlation",
        command="test",
        dry_run=True,
        result={"status": "planned", "token": "synthetic-redaction-value"},
        timestamp=1,
    )
    assert redact({"password": "value", "note": "Bearer abc.def"}) == {
        "password": "[REDACTED]",
        "note": "[REDACTED]",
    }
    code, output = invoke(capsys, config, "doctor", "--bundle")
    assert code == EXIT_OK
    bundle = Path(str(output["diagnostic_bundle"])).read_text(encoding="utf-8")
    assert "synthetic-redaction-value" not in bundle
    assert "[REDACTED]" in bundle
    assert output["metrics"] == {
        "schema_version": 1,
        "counters": {"operations.test.planned": 1},
    }


def test_configuration_rejects_repository_outside_allowlist(capsys: object, tmp_path: Path) -> None:
    config = local_example(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace('name = "demo"', 'name = "other"', 1),
        encoding="utf-8",
    )
    code, output = invoke(capsys, config, "inspect")
    assert code == EXIT_INVALID
    assert "not in repositories.allow" in str(output["error"])
