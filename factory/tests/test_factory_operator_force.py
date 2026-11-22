"""Operator force always results in work; only outside constraints stop it.

Owner rule (2026-11-21): the person at the CLI is right. Pacing the factory set
for itself (capacity, queue and review limits, cooldowns, budgets, pauses,
health history, dependency or blocked labels) never refuses a forced request.
Another worker's live claim is reported, never stolen.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/scripts'))

import factory_worker_slots as worker_slots  # noqa: E402


def load_controller(monkeypatch, name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / '.github/scripts/factory-work-controller.py'
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, controller)
    spec.loader.exec_module(controller)
    return controller


def issue(number, labels=(), state='open', body='', pull_request=False):
    payload = {'number': number, 'state': state, 'body': body,
               'created_at': '2026-11-20T00:00:00Z',
               'labels': [{'name': name} for name in labels]}
    if pull_request:
        payload['pull_request'] = {}
    return payload


def test_force_skips_every_self_imposed_worker_limit(monkeypatch):
    controller = load_controller(monkeypatch, 'factory_controller_force_limits')
    monkeypatch.setattr(controller, 'quota_pause_for',
                        lambda _w: pytest.fail('configured pause must not apply'))
    monkeypatch.setattr(controller, 'runtime_quota_block_reason',
                        lambda _w: pytest.fail('remembered cooldown must not apply'))
    monkeypatch.setattr(controller, 'budget_block_reason',
                        lambda _w: pytest.fail('daily budget must not apply'))
    monkeypatch.setattr(controller, 'omniroute_free_entry_capacity',
                        lambda: pytest.fail('shared entry cap must not apply'))
    monkeypatch.setattr(controller, 'worker_sources', lambda: {'7': 'opencode-free'})
    monkeypatch.setattr(controller, 'worker_has_active_lease', lambda _w: False)
    monkeypatch.setenv('FACTORY_HAS_OPENCODE_GO_CLIENT_ID', 'true')
    assert controller.worker_skip_reason('7', operator_force=True) is None


def test_force_still_reports_a_running_worker_and_missing_credentials(monkeypatch):
    controller = load_controller(monkeypatch, 'factory_controller_force_hard')
    monkeypatch.setattr(controller, 'worker_sources', lambda: {'7': 'nvidia'})
    monkeypatch.setattr(controller, 'worker_has_active_lease', lambda _w: True)
    monkeypatch.delenv('FACTORY_HAS_NVIDIA_API_KEY', raising=False)
    assert 'missing provider credentials' in controller.worker_skip_reason('7', operator_force=True)
    monkeypatch.setenv('FACTORY_HAS_NVIDIA_API_KEY', 'true')
    assert 'active lease' in controller.worker_skip_reason('7', operator_force=True)


def test_force_uses_an_operator_identity_when_regular_workers_are_busy(monkeypatch):
    controller = load_controller(monkeypatch, 'factory_controller_force_overflow')
    monkeypatch.setenv('FACTORY_SLOTS_PER_PROVIDER', '2')
    regular = [slot for slot in worker_slots.slots() if slot.source == 'opencode-free']
    overflow = worker_slots.operator_slots('opencode-free')
    assert [slot.index for slot in overflow][:2] == [2, 3]
    busy = {slot.worker for slot in regular}
    monkeypatch.setattr(controller, 'worker_skip_reason', lambda worker, **_kw: (
        f'Factory {worker} already has an active lease' if worker in busy else None
    ))
    assigned = []
    monkeypatch.setattr(controller, 'assign', lambda worker, **kw: (
        assigned.append(worker) or SimpleNamespace(
            kind='issue', number=42, stage='factory:building', conflicted=False)
    ))
    monkeypatch.setattr(controller, 'assignment_context',
                        lambda candidate, worker: {'worker': worker, 'number': candidate.number})
    launched = []
    monkeypatch.setattr(controller, 'run_gh', lambda argv: launched.append(argv))
    result = controller.dispatch_next('opencode-go', 'opencode-go/hy4-flash-free', force_slot=True)
    assert result['status'] == 'dispatched'
    assert result['worker'] == overflow[0].worker
    assert assigned == [overflow[0].worker]
    # The worker run accepts the overflow identity and its provider.
    assert worker_slots.resolve(overflow[0].worker).source == 'opencode-free'
    assert 'provider=opencode-free' in launched[0]


def test_forced_target_held_by_another_worker_is_reported_not_stolen(monkeypatch):
    controller = load_controller(monkeypatch, 'factory_controller_force_busy')
    monkeypatch.setattr(worker_slots, 'slots', lambda: [
        SimpleNamespace(source='opencode-free', index=0, worker='1000000000001'),
    ])
    monkeypatch.setattr(controller, 'worker_skip_reason', lambda worker, **_kw: None)
    monkeypatch.setattr(controller, 'is_authorized_fixed_worker', lambda _w: True)
    monkeypatch.setattr(controller, 'target_json',
                        lambda n: issue(n, ['factory', 'factory:1207147100200']))
    monkeypatch.setattr(controller, 'list_prs', lambda: [])
    monkeypatch.setattr(controller, 'acquire_lease',
                        lambda *a, **k: pytest.fail('no claim may be taken'))
    monkeypatch.setattr(controller, 'run_gh', lambda argv: pytest.fail('nothing may start'))
    result = controller.dispatch_next('opencode-go', force_slot=True, target='issue:3953')
    assert result['status'] == 'busy'
    assert result['number'] == 3953
    assert result['owner_worker'] == '1207147100200'


@pytest.mark.parametrize('labels', [
    ['factory', 'factory:unowned', 'factory:blocked'],
    ['factory', 'factory:unowned', 'ralph-status:blocked'],
    ['factory', 'factory:unowned'],
])
def test_forced_issue_ignores_blocked_and_backpressure_labels(monkeypatch, labels):
    controller = load_controller(monkeypatch, 'factory_controller_force_issue')
    monkeypatch.setattr(controller, 'target_json', lambda n: issue(n, labels))
    monkeypatch.setattr(controller, 'list_prs', lambda: [])
    monkeypatch.setattr(controller, 'issue_has_open_blocker',
                        lambda *a, **k: pytest.fail('dependencies must not hide a forced item'))
    candidate, action, stage = controller.forced_target_candidate('issue', 3953)
    assert (candidate.kind, candidate.number, action, stage) == (
        'issue', 3953, 'implement', 'factory:building')


def test_forced_issue_continues_its_existing_pull_request(monkeypatch):
    controller = load_controller(monkeypatch, 'factory_controller_force_linked')
    targets = {3953: issue(3953, ['factory:unowned']),
               77: issue(77, ['factory:unowned', 'factory:ready'], pull_request=True)}
    monkeypatch.setattr(controller, 'target_json', lambda n: targets[n])
    monkeypatch.setattr(controller, 'list_prs', lambda: [{'number': 77}])
    monkeypatch.setattr(controller, 'linked_issue_from_pr', lambda pr: 3953)
    monkeypatch.setattr(controller, 'pr_suppresses_issue_candidate', lambda *_a: True)
    monkeypatch.setattr(controller, 'live_pr_state', lambda n: {
        'mergeable': True, 'mergeable_state': 'clean', 'head': {'ref': 'factory/1-3953-x', 'sha': 'a' * 40},
    })
    candidate, action, stage = controller.forced_target_candidate('issue', 3953)
    # A ready PR has no stage action; force still produces work on it.
    assert (candidate.kind, candidate.number) == ('pr', 77)
    assert (action, stage) == ('review-findings', 'factory:changes-requested')


def test_forced_target_closed_or_manual_only_explains_itself(monkeypatch):
    controller = load_controller(monkeypatch, 'factory_controller_force_refusals')
    monkeypatch.setattr(controller, 'list_prs', lambda: [])
    monkeypatch.setattr(controller, 'target_json', lambda n: issue(n, state='closed'))
    with pytest.raises(RuntimeError, match='closed'):
        controller.forced_target_candidate('issue', 1)
    monkeypatch.setattr(controller, 'target_json', lambda n: issue(
        n, body='<!-- factory-execution:manual-only -->'))
    with pytest.raises(RuntimeError, match='manual-only'):
        controller.forced_target_candidate('issue', 1)


def test_forced_claim_records_operator_provenance_and_rolls_back(monkeypatch):
    controller = load_controller(monkeypatch, 'factory_controller_force_claim')
    labels = {3953: ['factory:unowned']}
    monkeypatch.setattr(controller, 'target_json', lambda n: issue(n, labels[n]))
    monkeypatch.setattr(controller, 'list_prs', lambda: [])
    monkeypatch.setattr(controller, 'worker_has_active_lease', lambda _w: False)
    monkeypatch.setattr(controller, 'acquire_lease', lambda *a: 'claim-token')

    def relabel(number, owner, stage=None, **_kw):
        labels[number] = [owner] + ([stage] if stage else [])
        return True
    monkeypatch.setattr(controller, 'replace_factory_labels', relabel)
    markers = []
    monkeypatch.setattr(controller, 'record_controller_lease_activity',
                        lambda number, worker, kind, **kw: markers.append((number, kw)))
    candidate = controller.assign_forced_target('9', ('issue', 3953))
    assert candidate.number == 3953
    assert labels[3953] == ['factory:9', 'factory:building']
    assert markers == [(3953, {'action': 'implement', 'source': 'operator', 'head': None})]

    labels[3953] = ['factory:unowned']
    released = []
    monkeypatch.setattr(controller, 'record_controller_lease_activity',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('comment failed')))
    monkeypatch.setattr(controller, 'release_lease', lambda *a: released.append(a))
    with pytest.raises(RuntimeError, match='comment failed'):
        controller.assign_forced_target('9', ('issue', 3953))
    assert labels[3953] == ['factory:unowned']
    assert released and released[0][2] == 'claim-token'


def test_worker_accepts_a_forced_pull_request_that_is_not_normally_executable(monkeypatch):
    controller = load_controller(monkeypatch, 'factory_controller_force_validate')
    context = {'worker': '9', 'kind': 'pr', 'number': 77, 'claim_id': 'c',
               'stage': 'factory:ci', 'head': 'a' * 40, 'branch': 'factory/9-1-x'}
    monkeypatch.setattr(controller, '_active_claim', lambda *a: ('c', [77]))
    monkeypatch.setattr(controller, 'target_json', lambda n: issue(n, ['factory:9', 'factory:ci']))
    monkeypatch.setattr(controller, 'live_pr_state', lambda n: {
        'head': {'sha': 'a' * 40, 'ref': 'factory/9-1-x'},
        'labels': [{'name': 'factory:9'}, {'name': 'factory:ci'}], 'mergeable': True,
    })
    monkeypatch.setattr(controller, 'candidate_is_live_executable', lambda _c: False)
    marker = '<!-- comic-pile-factory-controller-claim-v1:pr-77:9:1795300000:source=operator:action=ci-repair -->'
    monkeypatch.setattr(controller, 'gh_json', lambda argv: [[{'body': marker}]])
    monkeypatch.setattr(controller, 'comment_is_trusted', lambda _c: True)
    assert controller.validate_assignment('9', json.dumps(context)) == context
    # Without operator provenance the ordinary rule still applies.
    monkeypatch.setattr(controller, 'gh_json', lambda argv: [[{'body': marker.replace('source=operator:', '')}]])
    with pytest.raises(ValueError, match='waiting or no longer executable'):
        controller.validate_assignment('9', json.dumps(context))
