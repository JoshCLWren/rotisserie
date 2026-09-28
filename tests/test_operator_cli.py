from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

from rotisserie.cli import EXIT_INVALID, EXIT_OK, EXIT_REJECTED, main
from rotisserie.operator import OperationJournal, load_config, redact

EXAMPLE = Path(__file__).parents[1] / "examples" / "local"
GITHUB_FIXTURE = Path(__file__).parent / "fixtures" / "github" / "graph.json"


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


def test_decide_cli_projects_snapshot_without_initializing_graph_state(
    capsys: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path)

    code, output = invoke(
        capsys,
        config,
        "decide",
        "--snapshot",
        str(tmp_path / "graph.json"),
        "--revision",
        "comic-pile-snapshot-1",
        "--at",
        "10",
    )

    assert code == EXIT_OK
    decisions = output["evidence"]["decisions"]  # type: ignore[index]
    assert decisions["source"] == "rotisserie"
    assert decisions["revision"] == "comic-pile-snapshot-1"
    assert {item["dimension"] for item in decisions["observations"]} == {
        "eligibility",
        "ownership",
        "ranking",
        "review",
        "completion",
    }
    assert not (tmp_path / "state" / "graph-state.json").exists()


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


def test_dogfood_projection_and_plan_are_non_activating_and_durable(
    capsys: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace('host = "example.test"', 'host = "github.com"')
        .replace('owner = "rotisserie"', 'owner = "acme"')
        .replace('name = "demo"', 'name = "oven"'),
        encoding="utf-8",
    )
    payload = tmp_path / "github.json"
    shutil.copy(GITHUB_FIXTURE, payload)

    code, projected = invoke(
        capsys,
        config,
        "dogfood",
        "--stage",
        "fixture",
        "--payload",
        str(payload),
        "--at",
        "10",
    )
    assert code == EXIT_OK
    evidence = projected["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["stage"] == "fixture"
    assert evidence["remote_mutation"] is False
    assert evidence["activation_approved"] is False
    assert evidence["graph"] == {
        "schema_version": 1,
        "works": 3,
        "changes": 1,
        "leases": 0,
        "checks": 2,
        "reviews": 2,
    }

    code, planned = invoke(
        capsys,
        config,
        "dogfood",
        "--stage",
        "dry-run",
        "--payload",
        str(payload),
        "--at",
        "10",
        "--target",
        "issue:20",
        "--label",
        "rotisserie:canary",
    )
    assert code == EXIT_OK
    dry_run = planned["evidence"]
    assert isinstance(dry_run, dict)
    plans = dry_run["mutation_plans"]
    assert isinstance(plans, list)
    assert plans[0]["repository"] == "acme/oven"
    assert plans[0]["number"] == 20
    assert plans[0]["labels"] == ["rotisserie:canary"]
    assert not (tmp_path / "state" / "graph-state.json").exists()
    records = OperationJournal(tmp_path / "state").records()
    assert [record["command"] for record in records] == [
        "dogfood:fixture",
        "dogfood:dry-run",
    ]


def test_dogfood_rejects_mutation_options_before_dry_run(capsys: object, tmp_path: Path) -> None:
    config = local_example(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace('host = "example.test"', 'host = "github.com"')
        .replace('owner = "rotisserie"', 'owner = "acme"')
        .replace('name = "demo"', 'name = "oven"'),
        encoding="utf-8",
    )
    code, output = invoke(
        capsys,
        config,
        "dogfood",
        "--stage",
        "read-only",
        "--payload",
        str(GITHUB_FIXTURE),
        "--at",
        "10",
        "--target",
        "issue:20",
    )
    assert code == EXIT_INVALID
    assert "only valid for dry-run" in str(output["error"])


def test_dogfood_accepts_streamed_payload(
    capsys: object, monkeypatch: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace('host = "example.test"', 'host = "github.com"')
        .replace('owner = "rotisserie"', 'owner = "acme"')
        .replace('name = "demo"', 'name = "oven"'),
        encoding="utf-8",
    )
    payload = GITHUB_FIXTURE.read_bytes()

    class Stream:
        buffer = io.BytesIO(payload)

    monkeypatch.setattr(sys, "stdin", Stream())  # type: ignore[attr-defined]
    code, output = invoke(
        capsys,
        config,
        "dogfood",
        "--stage",
        "read-only",
        "--payload",
        "-",
        "--at",
        "10",
    )

    assert code == EXIT_OK
    evidence = output["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payload_sha256"]
    assert evidence["remote_mutation"] is False


def test_shadow_cli_reports_and_persists_adopter_divergence(capsys: object, tmp_path: Path) -> None:
    config = local_example(tmp_path)
    baseline = tmp_path / "legacy.json"
    candidate = tmp_path / "rotisserie.json"
    common = {
        "schema_version": 1,
        "revision": "comic-pile-snapshot-1",
        "observations": [
            {
                "dimension": "eligibility",
                "subject": "issue:10",
                "outcome": "eligible",
                "reasons": [],
                "rank": None,
            }
        ],
    }
    baseline.write_text(json.dumps({**common, "source": "comic-pile-factory"}))
    candidate.write_text(
        json.dumps(
            {
                **common,
                "source": "rotisserie",
                "observations": [
                    {
                        **common["observations"][0],  # type: ignore[index]
                        "outcome": "blocked",
                        "reasons": ["dependency"],
                    }
                ],
            }
        )
    )

    code, output = invoke(
        capsys,
        config,
        "shadow",
        "--baseline",
        str(baseline),
        "--candidate",
        str(candidate),
    )

    assert code == EXIT_REJECTED
    assert output["status"] == "diverged"
    evidence = output["evidence"]
    assert isinstance(evidence, dict)
    report = evidence["report"]
    assert isinstance(report, dict)
    assert report["matches"] is False
    assert [item["kind"] for item in report["divergences"]] == ["outcome", "reasons"]
    assert evidence["remote_mutation"] is False
    assert not (tmp_path / "state" / "graph-state.json").exists()
    assert OperationJournal(tmp_path / "state").records()[0]["command"] == "shadow"


def test_shadow_cli_accepts_one_stream_and_rejects_revision_mismatch(
    capsys: object, monkeypatch: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path)
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "rotisserie",
                "revision": "new",
                "observations": [],
            }
        )
    )

    class Stream:
        buffer = io.BytesIO(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "comic-pile-factory",
                    "revision": "old",
                    "observations": [],
                }
            ).encode()
        )

    monkeypatch.setattr(sys, "stdin", Stream())  # type: ignore[attr-defined]
    code, output = invoke(
        capsys,
        config,
        "shadow",
        "--baseline",
        "-",
        "--candidate",
        str(candidate),
    )

    assert code == EXIT_INVALID
    assert "same graph revision" in str(output["error"])


def test_shadow_project_cli_atomically_projects_and_compares_adopter_input(
    capsys: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path)
    revision = "comic-pile-snapshot-1"
    code, projected = invoke(
        capsys,
        config,
        "decide",
        "--snapshot",
        str(tmp_path / "graph.json"),
        "--revision",
        revision,
        "--at",
        "10",
    )
    assert code == EXIT_OK
    baseline_data = projected["evidence"]["decisions"]  # type: ignore[index]
    assert isinstance(baseline_data, dict)
    baseline_data["source"] = "comic-pile-factory"
    baseline = tmp_path / "legacy.json"
    baseline.write_text(json.dumps(baseline_data), encoding="utf-8")

    code, output = invoke(
        capsys,
        config,
        "shadow-project",
        "--baseline",
        str(baseline),
        "--snapshot",
        str(tmp_path / "graph.json"),
        "--revision",
        revision,
        "--at",
        "10",
    )

    assert code == EXIT_OK
    assert output["status"] == "match"
    evidence = output["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["baseline_sha256"]
    assert evidence["snapshot_sha256"]
    assert evidence["remote_mutation"] is False
    candidate = evidence["candidate"]
    report = evidence["report"]
    assert isinstance(candidate, dict)
    assert isinstance(report, dict)
    assert candidate["source"] == "rotisserie"
    assert report["matches"] is True
    assert not (tmp_path / "state" / "graph-state.json").exists()
    assert OperationJournal(tmp_path / "state").records()[-1]["command"] == "shadow-project"


def test_shadow_project_cli_rejects_revision_mismatch_before_projection(
    capsys: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path)
    baseline = tmp_path / "legacy.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "comic-pile-factory",
                "revision": "snapshot-old",
                "observations": [],
            }
        ),
        encoding="utf-8",
    )

    code, output = invoke(
        capsys,
        config,
        "shadow-project",
        "--baseline",
        str(baseline),
        "--snapshot",
        str(tmp_path / "graph.json"),
        "--revision",
        "snapshot-new",
        "--at",
        "10",
    )

    assert code == EXIT_INVALID
    assert "baseline must describe the requested graph revision" in str(output["error"])


def matching_shadow_report(revision: str) -> dict[str, object]:
    dimensions = [
        "completion",
        "eligibility",
        "ownership",
        "ranking",
        "recovery",
        "review",
    ]
    return {
        "schema_version": 1,
        "baseline": {"source": "comic-pile-factory", "revision": revision},
        "candidate": {"source": "rotisserie", "revision": revision},
        "compared": len(dimensions),
        "dimensions": dimensions,
        "matches": True,
        "divergences": [],
    }


def adoption_evidence(kind: str, *, control_revision: str = "controls-1") -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": kind,
        "lane": {"name": "issue-intake", "subjects": ["label:ready"]},
        "control_revision": control_revision,
        "from_stage": "canary",
        "to_stage": "legacy" if kind == "rollback_drill" else "canary",
    }


def test_adopt_cli_authorizes_bounded_canary_without_remote_mutation(
    capsys: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path)
    reports = []
    for revision in ("snapshot-1", "snapshot-2"):
        path = tmp_path / f"{revision}.json"
        path.write_text(json.dumps(matching_shadow_report(revision)))
        reports.append(path)
    rollback_evidence = tmp_path / "rollback.json"
    rollback_evidence.write_text(json.dumps(adoption_evidence("rollback_drill")))

    code, output = invoke(
        capsys,
        config,
        "adopt",
        "--report",
        str(reports[0]),
        "--report",
        str(reports[1]),
        "--lane",
        "issue-intake",
        "--subject",
        "label:ready",
        "--minimum-matching-runs",
        "2",
        "--evidence",
        str(rollback_evidence),
        "--control-revision",
        "controls-1",
        "--operator-approved",
    )

    assert code == EXIT_OK
    assert output["status"] == "authorized"
    evidence = output["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["decision"] == {
        "schema_version": 2,
        "action": "enter_canary",
        "authorized": True,
        "from_stage": "legacy",
        "to_stage": "canary",
        "lane": {"name": "issue-intake", "subjects": ["label:ready"]},
        "reasons": [],
    }
    transition = evidence["transition"]
    assert isinstance(transition, dict)
    assert transition == {
        "schema_version": 1,
        "operation_key": transition["operation_key"],
        "control_revision": "controls-1",
        "action": "enter_canary",
        "lane": {"name": "issue-intake", "subjects": ["label:ready"]},
        "expected_stage": "legacy",
        "target_stage": "canary",
    }
    assert str(transition["operation_key"]).startswith("adoption-transition:")
    assert len(evidence["report_sha256"]) == 2
    assert len(evidence["evidence_sha256"]) == 1
    assert evidence["control_revision"] == "controls-1"
    assert evidence["remote_mutation"] is False
    assert not (tmp_path / "state" / "graph-state.json").exists()


def test_adopt_cli_holds_without_approval_and_allows_evidence_free_rollback(
    capsys: object, tmp_path: Path
) -> None:
    config = local_example(tmp_path)

    code, held = invoke(
        capsys,
        config,
        "adopt",
        "--lane",
        "issue-intake",
        "--subject",
        "label:ready",
        "--control-revision",
        "controls-1",
    )
    assert code == EXIT_REJECTED
    assert held["status"] == "held"
    assert held["evidence"]["transition"] is None  # type: ignore[index]

    code, rollback = invoke(
        capsys,
        config,
        "adopt",
        "--lane",
        "issue-intake",
        "--subject",
        "label:ready",
        "--control-revision",
        "controls-1",
        "--current-stage",
        "canary",
        "--rollback-requested",
    )
    assert code == EXIT_OK
    assert rollback["evidence"]["decision"]["action"] == "rollback"  # type: ignore[index]
    assert rollback["evidence"]["transition"]["target_stage"] == "legacy"  # type: ignore[index]
