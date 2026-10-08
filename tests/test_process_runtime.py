"""Real-process verification of bounded provider-neutral worker execution."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rotisserie.adapters.process import ProcessExecutor, ProcessSpec
from rotisserie.application.process import (
    assignment_from_dict,
    assignment_to_dict,
    process_response,
    process_result,
    resume_from_dict,
)
from rotisserie.application.runtime import (
    Assignment,
    AssignmentId,
    AttemptEvent,
    AttemptId,
    AttemptState,
    Authority,
    AuthorityManifest,
    ExecutionResult,
    Outcome,
    ResumePacket,
    WorkerRuntime,
)
from rotisserie.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK, main
from rotisserie.domain import (
    Boundary,
    BoundaryId,
    BoundaryKind,
    Change,
    ChangeId,
    GraphSnapshot,
    Lease,
    LeaseId,
    RepositoryId,
    Revision,
    RevisionId,
    Work,
    Worker,
    WorkerId,
    WorkerInventory,
    WorkId,
)
from rotisserie.operator.config import ExecutionConfig, OperatorConfig, load_config
from rotisserie.operator.execution import execute_assignment
from rotisserie.operator.records import OperationJournal

REPOSITORY = RepositoryId("example.test", "acme", "project")
WORK = WorkId(REPOSITORY, "42")
WORKER = WorkerId("workers", "one")
ATTEMPT = AttemptId("attempt")


def assignment() -> Assignment:
    return Assignment(
        AssignmentId("assignment"),
        WORKER,
        "Perform the bounded task",
        AuthorityManifest(WORK, None, frozenset({Authority.READ}), int(time.time()) + 60),
        "Untrusted issue text",
        timeout_seconds=2,
    )


class Events:
    def __init__(self) -> None:
        self.items: list[AttemptEvent] = []

    def record(self, event: AttemptEvent) -> None:
        self.items.append(event)


class Cancel:
    def __init__(self, *, after: float | None = None) -> None:
        self.after = time.monotonic() + after if after is not None else None

    def cancelled(self) -> bool:
        return self.after is not None and time.monotonic() >= self.after


def bridge(tmp_path: Path, body: str, *, name: str = "bridge") -> ProcessSpec:
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, os, sys, time\nrequest = json.load(sys.stdin)\n" + body,
        encoding="utf-8",
    )
    return ProcessSpec(name, (sys.executable, str(script)))


def response(outcome: str = "succeeded") -> str:
    return (
        "print(json.dumps({'schema_version': 1, "
        "'assignment': request['assignment']['id'], 'attempt': request['attempt'], "
        f"'result': {{'outcome': {outcome!r}, 'detail': None, 'revision': None, "
        "'evidence': [], 'resume': None, 'retry_after_seconds': None}}))\n"
    )


def execute(
    spec: ProcessSpec,
    tmp_path: Path,
    *,
    assigned: Assignment | None = None,
    cancellation: Cancel | None = None,
    output_limit: int = 1_048_576,
) -> ExecutionResult:
    return ProcessExecutor(spec, tmp_path, output_limit=output_limit).execute(
        assigned or assignment(),
        ATTEMPT,
        cancellation=cancellation or Cancel(),
        events=Events(),
    )


def test_wire_round_trip_and_identity_binding() -> None:
    assigned = assignment()
    assert assignment_from_dict(assignment_to_dict(assigned)) == assigned
    packet = ResumePacket(assigned.id, ATTEMPT, WORKER, "ready to resume", 10)
    assert resume_from_dict(packet.to_dict()) == packet
    result = ExecutionResult(Outcome.NO_DIFF, resume=packet)
    encoded = process_response(assigned, ATTEMPT, result)
    assert process_result(encoded, assigned, ATTEMPT) == result
    with pytest.raises(ValueError, match="another assignment"):
        process_result(encoded, replace(assigned, id=AssignmentId("other")), ATTEMPT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("timeout_seconds", True),
        ("id", "ghp_test_placeholder"),
        ("unexpected", "value"),
    ],
)
def test_wire_rejects_invalid_or_secret_shaped_assignment(field: str, value: object) -> None:
    raw = assignment_to_dict(assignment())
    raw[field] = value
    with pytest.raises(ValueError):
        assignment_from_dict(raw)


def test_real_process_has_explicit_request_and_no_inherited_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROTISSERIE_TEST_PRIVATE_VALUE", "sentinel")
    spec = bridge(
        tmp_path,
        "assert 'ROTISSERIE_TEST_PRIVATE_VALUE' not in os.environ\n"
        "assert request['assignment']['authority']['permissions'] == ['read']\n"
        "assert '<repository-content trusted=\"false\">' in request['prompt']\n"
        f"assert os.getcwd() == {str(tmp_path)!r}\n"
        "assert not os.path.exists(os.path.join(os.environ['HOME'], '.gitconfig'))\n" + response(),
    )
    assert execute(spec, tmp_path).outcome is Outcome.SUCCEEDED


@pytest.mark.parametrize(
    "body,expected",
    [
        ("print('not JSON')\n", Outcome.MALFORMED_OUTPUT),
        ("print('{}')\n", Outcome.MALFORMED_OUTPUT),
        ("print('ghp_test_placeholder', file=sys.stderr)\nsys.exit(1)\n", Outcome.FAILED),
        ("sys.exit(0)\n", Outcome.MALFORMED_OUTPUT),
        ("print('x' * 100000)\n", Outcome.MALFORMED_OUTPUT),
        (response("unknown"), Outcome.MALFORMED_OUTPUT),
    ],
)
def test_process_failure_does_not_return_raw_output(
    tmp_path: Path,
    body: str,
    expected: Outcome,
) -> None:
    result = execute(bridge(tmp_path, body), tmp_path, output_limit=4096)
    assert result.outcome is expected
    assert "ghp_" not in str(result)
    assert "not JSON" not in str(result)


def test_invalid_revision_and_resume_never_escape_process_boundary(tmp_path: Path) -> None:
    assigned = assignment()
    packet = ResumePacket(AssignmentId("other"), ATTEMPT, WORKER, "checkpoint", 10)
    malformed = process_response(
        assigned, ATTEMPT, ExecutionResult(Outcome.SUCCEEDED, resume=packet)
    )
    spec = bridge(tmp_path, f"print({json.dumps(malformed)!r})\n")
    assert execute(spec, tmp_path, assigned=assigned).outcome is Outcome.MALFORMED_OUTPUT
    revision = RevisionId(RepositoryId("example.test", "other", "repo"), "abc")
    escaped = process_response(
        assigned, ATTEMPT, ExecutionResult(Outcome.SUCCEEDED, revision=revision)
    )
    spec = bridge(tmp_path, f"print({json.dumps(escaped)!r})\n")
    assert execute(spec, tmp_path, assigned=assigned).outcome is Outcome.MALFORMED_OUTPUT


@pytest.mark.parametrize("outcome", ["no_diff", "failed", "timed_out", "cancelled"])
def test_terminal_process_outcomes_do_not_fallback(tmp_path: Path, outcome: str) -> None:
    marker = tmp_path / "fallback-called"
    first = ProcessExecutor(bridge(tmp_path, response(outcome), name="first"), tmp_path)
    second = ProcessExecutor(
        bridge(tmp_path, f"open({str(marker)!r}, 'w').close()\n" + response(), name="second"),
        tmp_path,
    )
    result = WorkerRuntime((first, second), Events()).run(
        assignment(),
        at=int(time.time()),
        cancellation=Cancel(),
    )
    assert result.result.outcome == outcome
    assert not marker.exists()


@pytest.mark.parametrize("outcome", ["rate_limited", "unavailable"])
def test_transient_outcomes_fallback_in_configured_order(tmp_path: Path, outcome: str) -> None:
    first = ProcessExecutor(bridge(tmp_path, response(outcome), name="first"), tmp_path)
    second = ProcessExecutor(bridge(tmp_path, response(), name="second"), tmp_path)
    result = WorkerRuntime((first, second), Events()).run(
        assignment(),
        at=int(time.time()),
        cancellation=Cancel(),
    )
    assert result.result.outcome is Outcome.SUCCEEDED
    assert result.executor == "second"
    assert len(result.attempts) == 2


def test_timeout_and_cancellation_kill_process_group_and_emit_heartbeats(tmp_path: Path) -> None:
    spec = bridge(tmp_path, "time.sleep(30)\n")
    events = Events()
    executor = ProcessExecutor(spec, tmp_path, heartbeat_seconds=0.02)
    started = time.monotonic()
    result = executor.execute(assignment(), ATTEMPT, cancellation=Cancel(after=0.2), events=events)
    assert result.outcome is Outcome.CANCELLED
    assert time.monotonic() - started < 3
    assert any(event.state is AttemptState.HEARTBEAT for event in events.items)
    assert (
        execute(spec, tmp_path, assigned=replace(assignment(), timeout_seconds=1)).outcome
        is Outcome.TIMED_OUT
    )


def test_authority_expiry_is_enforced_even_with_longer_timeout(tmp_path: Path) -> None:
    spec = bridge(tmp_path, "time.sleep(30)\n")
    assigned = assignment()
    assigned = replace(
        assigned, authority=replace(assigned.authority, expires_at=int(time.time()) + 1)
    )
    started = time.monotonic()
    assert execute(spec, tmp_path, assigned=assigned).outcome is Outcome.TIMED_OUT
    assert time.monotonic() - started < 2


def test_background_descendant_cannot_survive_successful_bridge_exit(tmp_path: Path) -> None:
    marker = tmp_path / "escaped"
    program = f"import time; time.sleep(0.5); open({str(marker)!r}, 'w').close()"
    spec = bridge(
        tmp_path,
        "import subprocess\n"
        f"subprocess.Popen([sys.executable, '-c', {program!r}], stdout=subprocess.DEVNULL)\n"
        + response(),
    )
    assert execute(spec, tmp_path).outcome is Outcome.SUCCEEDED
    time.sleep(0.7)
    assert not marker.exists()


def operator(tmp_path: Path, spec: ProcessSpec, assigned: Assignment) -> OperatorConfig:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "--quiet", str(worktree)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "remote",
            "add",
            "origin",
            "https://example.test/acme/project.git",
        ],
        check=True,
    )
    graph = GraphSnapshot(
        works=(Work(WORK, "Task"),),
        workers=(Worker(WORKER),),
        leases=(
            Lease(
                LeaseId("lease"), WORK, WORKER, int(time.time()) - 1, assigned.authority.expires_at
            ),
        ),
    )
    source = tmp_path / "graph.json"
    source.write_text(json.dumps(graph.to_dict()), encoding="utf-8")
    return OperatorConfig(
        REPOSITORY,
        frozenset({REPOSITORY}),
        source,
        tmp_path / "state",
        workers=WorkerInventory(frozenset({WORKER})),
        execution=ExecutionConfig(worktree, (spec,), enabled=True),
    )


def test_completed_receipt_suppresses_duplicate_launch_and_identity_reuse(tmp_path: Path) -> None:
    marker = tmp_path / "launches"
    spec = bridge(tmp_path, f"open({str(marker)!r}, 'a').write('launch\\n')\n" + response())
    assigned = assignment()
    config = operator(tmp_path, spec, assigned)
    journal = OperationJournal(config.state_directory)
    first = execute_assignment(assigned, config, journal, "one")
    second = execute_assignment(assigned, config, journal, "two")
    assert not first["replayed"] and second["replayed"]
    assert marker.read_text().splitlines() == ["launch"]
    with pytest.raises(ValueError, match="reused"):
        execute_assignment(
            replace(assigned, objective="Changed objective"), config, journal, "three"
        )


def test_execute_refuses_missing_lease_wrong_repo_worker_or_checkout(tmp_path: Path) -> None:
    assigned = assignment()
    config = operator(tmp_path, bridge(tmp_path, response()), assigned)
    journal = OperationJournal(config.state_directory)
    with pytest.raises(ValueError, match="outside configured scope"):
        execute_assignment(
            replace(
                assigned,
                authority=replace(
                    assigned.authority,
                    work=WorkId(RepositoryId("example.test", "other", "repo"), "42"),
                ),
            ),
            config,
            journal,
            "one",
        )
    with pytest.raises(ValueError, match="inventory"):
        execute_assignment(
            replace(assigned, worker=WorkerId("workers", "retired")), config, journal, "one"
        )
    config.snapshot.write_text(
        json.dumps(
            GraphSnapshot(
                works=(Work(WORK, "Task"),),
                workers=(Worker(WORKER),),
            ).to_dict()
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="active lease"):
        execute_assignment(assigned, config, journal, "one")


def test_worktree_mismatch_never_launches_bridge(tmp_path: Path) -> None:
    assigned = assignment()
    marker = tmp_path / "launched"
    config = operator(
        tmp_path, bridge(tmp_path, f"open({str(marker)!r}, 'w').close()\n" + response()), assigned
    )
    assert config.execution is not None
    subprocess.run(
        [
            "git",
            "-C",
            str(config.execution.worktree),
            "remote",
            "set-url",
            "origin",
            "https://example.test/other/repo.git",
        ],
        check=True,
    )
    with pytest.raises(ValueError, match="configured target"):
        execute_assignment(assigned, config, OperationJournal(config.state_directory), "one")
    assert not marker.exists()


def test_interrupted_receipt_requires_explicit_matching_resume(tmp_path: Path) -> None:
    assigned = assignment()
    config = operator(tmp_path, bridge(tmp_path, response()), assigned)
    journal = OperationJournal(config.state_directory)
    execute_assignment(assigned, config, journal, "one")
    receipt = next((config.state_directory / "executions").glob("*.json"))
    raw = json.loads(receipt.read_text())
    raw["status"] = "started"
    for field in ("executor", "attempts", "result"):
        del raw[field]
    receipt.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="explicit resume"):
        execute_assignment(assigned, config, journal, "two")
    packet = ResumePacket(assigned.id, ATTEMPT, WORKER, "operator inspected interrupted task", 10)
    result = execute_assignment(assigned, config, journal, "two", resume=packet)
    assert result["result"]["outcome"] == "succeeded"


def cli_config(tmp_path: Path, spec: ProcessSpec, *, enabled: bool = True) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'schema_version = 1\n[repository]\nhost = "example.test"\n'
        'owner = "acme"\nname = "project"\n'
        '[repositories]\nallow = [{host="example.test", owner="acme", name="project"}]\n'
        '[local]\nsnapshot = "graph.json"\nstate_directory = "state"\n'
        '[workers]\nexpected = ["workers:one"]\n'
        f'[execution]\nenabled = {str(enabled).lower()}\nworktree = "worktree"\n'
        f'[[execution.executors]]\nname = "{spec.name}"\nargv = {json.dumps(spec.argv)}\n',
        encoding="utf-8",
    )
    return path


def invoke(capsys: Any, config: Path, *arguments: str) -> tuple[int, dict[str, Any]]:
    code = main(["--config", str(config), "execute", *arguments])
    return code, json.loads(capsys.readouterr().out)


def test_cli_dry_run_starts_no_subprocess_and_initializes_no_graph(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = ProcessSpec("bridge", ("/missing/executable",))
    config = cli_config(tmp_path, spec)
    assigned = tmp_path / "assignment.json"
    assigned.write_text(json.dumps(assignment_to_dict(assignment())))

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry run attempted a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    code, output = invoke(capsys, config, "--assignment", str(assigned))
    assert code == EXIT_OK and output["status"] == "planned"
    assert not (tmp_path / "state" / "graph-state.json").exists()
    assert not (tmp_path / "state" / "executions").exists()


@pytest.mark.parametrize("outcome,expected", [("succeeded", EXIT_OK), ("failed", EXIT_FAILED)])
def test_cli_runs_public_contract_and_returns_meaningful_exit_code(
    tmp_path: Path,
    capsys: Any,
    outcome: str,
    expected: int,
) -> None:
    assigned = assignment()
    spec = bridge(tmp_path, response(outcome))
    operator(tmp_path, spec, assigned)
    config = cli_config(tmp_path, spec)
    path = tmp_path / "assignment.json"
    path.write_text(json.dumps(assignment_to_dict(assigned)))
    code, output = invoke(capsys, config, "--assignment", str(path), "--apply")
    assert code == expected
    assert output["result"]["result"]["outcome"] == outcome
    assert load_config(config).execution is not None


def test_cli_requires_execution_gate_independent_of_local_mutations(
    tmp_path: Path, capsys: Any
) -> None:
    spec = bridge(tmp_path, response())
    config = cli_config(tmp_path, spec, enabled=False)
    path = tmp_path / "assignment.json"
    path.write_text(json.dumps(assignment_to_dict(assignment())))
    code, output = invoke(capsys, config, "--assignment", str(path), "--apply")
    assert code == EXIT_INVALID and "execution is disabled" in output["error"]


def test_concurrent_caller_cannot_launch_same_target_with_another_assignment(
    tmp_path: Path,
) -> None:
    assigned = assignment()
    marker = tmp_path / "started"
    spec = bridge(tmp_path, f"open({str(marker)!r}, 'w').close()\ntime.sleep(30)\n" + response())
    operator(tmp_path, spec, assigned)
    config = cli_config(tmp_path, spec)
    path = tmp_path / "assignment.json"
    path.write_text(json.dumps(assignment_to_dict(assigned)))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rotisserie",
            "--config",
            str(config),
            "execute",
            "--assignment",
            str(path),
            "--apply",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        with pytest.raises(ValueError, match="already executing"):
            execute_assignment(
                replace(assigned, id=AssignmentId("another")),
                load_config(config),
                OperationJournal(tmp_path / "state"),
                "two",
            )
        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=5)
        assert not error
        assert json.loads(output)["result"]["result"]["outcome"] == "cancelled"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_secret_shaped_evidence_is_rejected_before_persistence() -> None:
    raw = process_response(assignment(), ATTEMPT, ExecutionResult(Outcome.SUCCEEDED))
    raw["result"]["evidence"] = [{"kind": "note", "source": "ghp_test_placeholder"}]  # type: ignore[index]
    with pytest.raises(ValueError, match="credential-like"):
        process_result(raw, assignment(), ATTEMPT)


def test_hostile_stdout_is_not_in_receipts_or_journal(tmp_path: Path) -> None:
    assigned = assignment()
    body = response().replace("'detail': None", "'detail': 'ghp_test_placeholder'")
    config = operator(tmp_path, bridge(tmp_path, body), assigned)
    result = execute_assignment(assigned, config, OperationJournal(config.state_directory), "one")
    assert result["result"]["outcome"] == "malformed_output"
    for path in config.state_directory.rglob("*.json*"):
        assert "ghp_" not in path.read_text()


def test_exact_head_assignment_is_verified_against_graph_and_worktree(tmp_path: Path) -> None:
    assigned = assignment()
    config = operator(tmp_path, bridge(tmp_path, response()), assigned)
    assert config.execution is not None
    worktree = config.execution.worktree
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True
    ).strip()
    revision = RevisionId(REPOSITORY, head)
    change = ChangeId(REPOSITORY, "7")
    assigned = replace(
        assigned, change=change, authority=replace(assigned.authority, revision=revision)
    )
    raw = json.loads(config.snapshot.read_text())
    snapshot = replace(
        GraphSnapshot.from_dict(raw),
        revisions=(Revision(revision),),
        changes=(Change(change, WORK, revision, WORKER),),
    )
    config.snapshot.write_text(json.dumps(snapshot.to_dict()))
    journal = OperationJournal(config.state_directory)
    stale = replace(
        assigned, authority=replace(assigned.authority, revision=RevisionId(REPOSITORY, "old"))
    )
    with pytest.raises(ValueError, match="exact head"):
        execute_assignment(stale, config, journal, "stale")
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "advance",
        ],
        check=True,
    )
    with pytest.raises(ValueError, match="worktree head"):
        execute_assignment(assigned, config, journal, "changed-checkout")


def test_unsatisfied_human_boundary_refuses_process_execution(tmp_path: Path) -> None:
    assigned = assignment()
    config = operator(tmp_path, bridge(tmp_path, response()), assigned)
    snapshot = GraphSnapshot.from_dict(json.loads(config.snapshot.read_text()))
    boundary = Boundary(BoundaryId("human"), WORK, BoundaryKind.HUMAN_APPROVAL)
    config.snapshot.write_text(json.dumps(replace(snapshot, boundaries=(boundary,)).to_dict()))
    with pytest.raises(ValueError, match="human boundary"):
        execute_assignment(assigned, config, OperationJournal(config.state_directory), "one")


def test_hard_killed_operator_leaves_bridge_lock_and_durable_process_identity(
    tmp_path: Path,
) -> None:
    assigned = assignment()
    marker = tmp_path / "started"
    spec = bridge(tmp_path, f"open({str(marker)!r}, 'w').close()\ntime.sleep(30)\n" + response())
    operator(tmp_path, spec, assigned)
    config = cli_config(tmp_path, spec)
    path = tmp_path / "assignment.json"
    path.write_text(json.dumps(assignment_to_dict(assigned)))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rotisserie",
            "--config",
            str(config),
            "execute",
            "--assignment",
            str(path),
            "--apply",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    group: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        receipt = next((tmp_path / "state" / "executions").glob("*.json"))
        raw = json.loads(receipt.read_text())
        assert raw["status"] == "started" and isinstance(raw["process_group"], int)
        group = raw["process_group"]
        process.kill()
        process.wait(timeout=5)
        with pytest.raises(ValueError, match="already executing"):
            execute_assignment(
                assigned, load_config(config), OperationJournal(tmp_path / "state"), "two"
            )
    finally:
        if group is not None:
            os.killpg(group, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
            process.wait()
