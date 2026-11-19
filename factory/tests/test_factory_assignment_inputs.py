"""Fenced Actions assignment handoffs never reconstruct a worker's queue."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def controller():
    """Load an isolated trusted controller for boundary tests."""
    spec = importlib.util.spec_from_file_location(
        'assignment_inputs_controller', ROOT / '.github/scripts/factory-work-controller.py'
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def assignment():
    """Use a descriptor with no repository snapshot or credentials."""
    return {'kind': 'pr', 'number': 9001, 'worker': '54', 'claim_id': 'original',
            'head': 'a' * 40, 'branch': 'local/continue', 'stage': 'factory:review'}


def test_assignment_validation_reads_only_target(controller, assignment, monkeypatch):
    """A valid handoff uses its lease and exact target; no inventory read is allowed."""
    monkeypatch.setattr(controller, 'worker_claim', lambda *_, **__: ('original', [9001]))
    monkeypatch.setattr(controller, 'list_issues', lambda: pytest.fail('global issue discovery'))
    monkeypatch.setattr(controller, 'list_prs', lambda: pytest.fail('global PR discovery'))
    monkeypatch.setattr(controller, 'target_json', lambda _: {
        'state': 'open', 'labels': [{'name': 'factory:54'}]})
    monkeypatch.setattr(controller, 'live_pr_state', lambda _: {
        'head': {'sha': 'a' * 40, 'ref': 'local/continue'},
        'labels': [{'name': 'factory:review'}]})
    monkeypatch.setattr(controller, 'candidate_is_live_executable', lambda _: True)
    assert controller.validate_assignment('54', json.dumps(assignment)) == assignment


@pytest.mark.parametrize('change', ['worker', 'claim', 'head', 'ci', 'owner', 'stage'])
def test_assignment_rejects_stale_or_ineligible_context(controller, assignment, monkeypatch, change):
    """Neither stale inputs nor a running CI job may allocate a model session."""
    monkeypatch.setattr(controller, 'worker_claim', lambda *_, **__: (
        'newer' if change == 'claim' else 'original', [9001]))
    monkeypatch.setattr(controller, 'target_json', lambda _: {
        'state': 'open', 'labels': [{'name': 'factory:55' if change == 'owner' else 'factory:54'}]})
    monkeypatch.setattr(controller, 'live_pr_state', lambda _: {
        'head': {'sha': ('b' if change == 'head' else 'a') * 40, 'ref': 'local/continue'},
        'labels': [{'name': 'factory:changes-requested' if change == 'stage' else 'factory:review'}]})
    monkeypatch.setattr(controller, 'candidate_is_live_executable', lambda _: change != 'ci')
    with pytest.raises(ValueError):
        controller.validate_assignment('55' if change == 'worker' else '54', json.dumps(assignment))


def test_stale_completion_never_reads_or_releases_new_owner(controller, monkeypatch):
    """An old completion cannot adopt the newer claim token during cleanup."""
    monkeypatch.setattr(controller, 'worker_claim', lambda *_: ('new', [9001]))
    monkeypatch.setattr(controller, 'target_json', lambda _: pytest.fail('stale GitHub mutation'))
    monkeypatch.setattr(controller, 'release_lease', lambda *_: pytest.fail('new lease released'))
    summary = {}
    assert controller.release_worker('54', 'post-session-exit-run-123',
                                     expected_claim='old', details=summary) == []
    assert summary['status'] == 'stale_completion'


def test_unfenced_completion_cannot_release_a_claim(controller, monkeypatch):
    """Old pre-migration cleanup must leave recovery to lease expiration."""
    monkeypatch.setattr(controller, 'worker_claim', lambda *_: ('new', [9001]))
    monkeypatch.setattr(controller, 'target_json', lambda _: pytest.fail('unfenced write'))
    summary = {}
    assert controller.release_worker('54', 'post-session-exit-run-123', details=summary) == []
    assert summary['status'] == 'missing_claim'


def test_dispatcher_deploy_does_not_cancel_work_in_progress():
    """A control-plane deployment must preserve useful active worker sessions."""
    workflow = (ROOT / '.github/workflows/fixed-model-factory-dispatch.yml').read_text()
    assert 'gh run cancel' not in workflow
    assert 'release_cancelled_worker_leases' not in workflow


def test_trusted_validation_and_health_outputs_are_under_worker_lock():
    """A queued duplicate validates after acquiring its reusable worker lock."""
    import yaml

    workflow = yaml.safe_load((ROOT / '.github/workflows/free-model-factory-run.yml').read_text())
    assert workflow['concurrency']['group'] == 'fixed-model-factory-${{ inputs.worker }}'
    preparation = workflow['jobs']['validate_assignment']
    factory = workflow['jobs']['factory']
    assert factory['needs'] == 'validate_assignment'
    assert preparation['outputs']['exclusions'] == '${{ steps.prepare.outputs.exclusions }}'
    assert 'NEON_API_KEY' not in json.dumps(factory)
    assert 'FACTORY_STATE_DSN_FILE' not in json.dumps(factory)
    assert 'validate-assignment' in json.dumps(preparation)


def test_assigned_worker_general_cleanup_does_not_scan_or_mutate():
    """Exercise startup/EXIT cleanup with a dispatcher assignment present."""
    import os
    import re
    import subprocess

    source = (ROOT / '.github/scripts/free-model-factory-worker-primitives.sh').read_text()
    match = re.search(r'(?ms)^release_owned_targets\(\) \{\n.*?^\}', source)
    assert match
    script = match.group(0) + '\nlog() { :; }\ngh() { exit 99; }\nrelease_owned_targets session-end-handoff'
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                            env={**os.environ, 'FACTORY_ASSIGNMENT': '{"claim_id":"old"}'})
    assert result.returncode == 0, result.stderr


def test_fork_route_rejects_a_newer_head_before_review():
    """Static fork review must remain bound to the dispatcher-selected commit."""
    import os
    import re
    import subprocess

    source = (ROOT / '.github/scripts/factory-fork-worker.sh').read_text()
    match = re.search(r'(?ms)^factory_route_fork_assignment\(\) \{\n.*?^\}', source)
    assert match
    script = match.group(0) + '''
MODE=pr
NUMBER=9001
log() { :; }
gh() { printf '%s' "$TEST_PR"; }
factory_route_fork_assignment
'''
    payload = {'head': {'sha': 'b' * 40, 'ref': 'branch', 'repo': {'full_name': 'fork/repo'}},
               'base': {'repo': {'full_name': 'owner/repo'}}}
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                            env={**os.environ, 'FACTORY_ASSIGNMENT': json.dumps({'head': 'a' * 40}),
                                 'GITHUB_REPOSITORY': 'owner/repo', 'TEST_PR': json.dumps(payload)})
    assert result.returncode == 2
