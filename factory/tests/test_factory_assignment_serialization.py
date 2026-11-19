"""Regression coverage for factory assignment-writer serialization.

Incident #2727 proved that independent workflow concurrency groups let two
controllers observe the same worker as idle and lease unrelated targets to it.
Every workflow with assignment authority must therefore share one repository-
wide serialization group.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ASSIGNMENT_GROUP = "fixed-model-factory-dispatch"


def _load_workflow(filename: str) -> dict:
    """Parse a workflow file with a real YAML parser.

    The workflow surface is intentionally flat: every assignment writer carries
    exactly one workflow-level ``concurrency`` block and one ``jobs:`` mapping.
    Parsing with a YAML parser instead of string splitting keeps the test robust
    against indentation changes, extra concurrency blocks, and reordered keys.
    """
    import yaml

    with (WORKFLOWS / filename).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _ingress_group(workflow: dict) -> str:
    """Return the workflow-level concurrency group expression, or ""."""
    concurrency = workflow.get("concurrency") or {}
    return str(concurrency.get("group", ""))


def _ingress_queue(workflow: dict) -> str:
    """Return the workflow-level concurrency queue mode, or ""."""
    concurrency = workflow.get("concurrency") or {}
    return str(concurrency.get("queue", ""))


def _ingress_cancel(workflow: dict) -> bool:
    """Return the workflow-level cancel-in-progress flag, defaulting True."""
    concurrency = workflow.get("concurrency") or {}
    return bool(concurrency.get("cancel-in-progress", True))


def _job_group(workflow: dict) -> str:
    """Return the first job's concurrency group expression, or ""."""
    jobs = workflow.get("jobs") or {}
    for job in jobs.values():
        concurrency = job.get("concurrency") or {}
        return str(concurrency.get("group", ""))
    return ""


def _job_queue(workflow: dict) -> str:
    """Return the first job's concurrency queue mode, or ""."""
    jobs = workflow.get("jobs") or {}
    for job in jobs.values():
        concurrency = job.get("concurrency") or {}
        return str(concurrency.get("queue", ""))
    return ""


def _job_cancel(workflow: dict) -> bool:
    """Return the first job's cancel-in-progress flag, defaulting True."""
    jobs = workflow.get("jobs") or {}
    for job in jobs.values():
        concurrency = job.get("concurrency") or {}
        return bool(concurrency.get("cancel-in-progress", True))
    return True


def test_all_factory_assignment_writers_share_one_concurrency_group() -> None:
    """No assignment-capable workflow may mutate leases outside the shared lock."""
    writers = {
        "fixed-model-factory-dispatch.yml": 'python3 "$controller" assign --worker "$worker"',
    }

    for filename, assignment_marker in writers.items():
        text = (WORKFLOWS / filename).read_text(encoding="utf-8")
        assert assignment_marker in text, f"{filename} no longer matches its assignment marker"
        assert ASSIGNMENT_GROUP in text, (
            f"{filename} can assign factory work without the shared serialization group"
        )


def test_completion_signal_has_no_assignment_authority_or_writer_lock() -> None:
    """Signals cannot revive the incident's second assignment authority."""
    completion = (WORKFLOWS / "factory-completion-drain.yml").read_text()
    assert " assign --worker " not in completion
    assert " release --worker " not in completion
    assert _job_group(_load_workflow("factory-completion-drain.yml")) == ""
    assert not (WORKFLOWS / "fixed-model-factory-capacity-refill.yml").exists()


def test_all_shared_assignment_workflows_preserve_pending_reconciliation() -> None:
    """All writers must opt into FIFO queuing; a single-mode entrant evicts waiters."""
    # Regression: migration-lane release runs were cancelled repeatedly while
    # dispatcher and completion work arrived in the same shared group.
    writers = (
        "fixed-model-factory-dispatch.yml",
        "factory-migration-lane-release.yml",
        "free-model-factory-dispatch.yml",
    )
    for filename in writers:
        workflow = _load_workflow(filename)
        ingress = _ingress_group(workflow)
        writer = _job_group(workflow)
        assert _ingress_queue(workflow) == "single", filename
        assert _ingress_cancel(workflow) is False, filename
        # The shared assignment group must not be used at workflow ingress level; it is only allowed in the job section.
        assert ingress != ASSIGNMENT_GROUP, filename
        assert writer == ASSIGNMENT_GROUP, filename
        assert _job_queue(workflow) == "max", filename
        assert _job_cancel(workflow) is False, filename
