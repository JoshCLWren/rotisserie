"""Model-first factory selection uses existing leases and live catalog membership."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/scripts'))
import factory_manual_model as exact


def test_model_first_chooses_canonical_provider_worker_and_real_pr(monkeypatch):
    import factory_completion_controller as completion
    spec = importlib.util.spec_from_file_location(
        'factory_work_controller', ROOT / '.github/scripts/factory-work-controller.py'
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'factory_work_controller', controller)
    spec.loader.exec_module(controller)
    import factory_worker_slots as worker_slots
    monkeypatch.setattr(worker_slots, 'slots', lambda: [
        SimpleNamespace(source='nvidia', index=0, worker='1004230852000'),
        SimpleNamespace(source='opencode-free', index=0, worker='1327811785100'),
        SimpleNamespace(source='opencode-free', index=1, worker='1327811785101'),
    ])
    monkeypatch.setattr(completion, 'durable_factory_state', lambda: ({}, {}))
    monkeypatch.setattr(completion, 'load_manifest_candidates', lambda: [])
    monkeypatch.setattr(completion, 'capacity_report', lambda *_a, **_kw: {
        'executable_candidates': [
            {'worker': '1004230852000'}, {'worker': '1327811785101'},
            {'worker': '1327811785100'},
        ]
    })
    monkeypatch.setattr(controller, 'worker_sources', lambda: {
        '1004230852000': 'nvidia',
        '1327811785101': 'opencode-free', '1327811785100': 'opencode-free',
    })
    invoked = []
    monkeypatch.setattr(controller, 'assign', lambda worker, named=False: (
        invoked.append(worker)
        or (None if worker.endswith('00') else SimpleNamespace(
            kind='issue', number=5147, stage='factory:building', conflicted=False))
    ))
    monkeypatch.setattr(controller, 'assignment_context', lambda c, worker: {
        'worker': worker, 'claim_id': 'synthetic', 'kind': c.kind, 'number': c.number,
    })
    launched = []
    monkeypatch.setattr(controller, 'run_gh', lambda argv: launched.append(argv))

    selected = controller.dispatch_next('opencode-go', 'opencode-go/hy4-flash-free')
    assert isinstance(selected.pop('dispatched_at'), int)
    assert selected == {
        'status': 'dispatched', 'kind': 'issue', 'number': 5147,
        'action': 'implement', 'worker': '1327811785101',
        'provider': 'opencode-free', 'model': 'opencode-go/hy4-flash-free',
    }
    assert invoked == ['1327811785100', '1327811785101']
    assert '-f' in launched[0]
    assert 'provider=opencode-free' in launched[0]
    assert 'model=opencode-go/hy4-flash-free' in launched[0]


def test_unknown_provider_and_runtime_prefix_fail_without_leasing(monkeypatch):
    with pytest.raises(ValueError, match='does not support exact-model selection'):
        exact.validate_model_syntax('not-a-provider', 'not-a-provider/some-model')
    with pytest.raises(ValueError, match='does not belong'):
        exact.validate_model_syntax('opencode-free', 'opencode/paid')


def test_opencode_go_live_catalog_must_be_zero_cost_and_outside_neon_cooldown(monkeypatch):
    from factory_opencode_go import PREFERRED
    free = {'type': 'chat', 'capabilities': {'tools': {'supported': True}},
            'cost': {'text': {'input': {'price': 0}, 'output': {'price': 0}}}}
    paid = {'type': 'chat', 'capabilities': {'tools': {'supported': True}},
            'cost': {'text': {'input': {'price': 1}, 'output': {'price': 1}}}}
    monkeypatch.setattr('factory_opencode_go.catalog', lambda: {
        'opencode-go': {'models': {PREFERRED: free, 'paid-one': paid}},
    })
    selected = exact.select_exact('opencode-free', f'opencode-go/{PREFERRED}', {})
    assert selected['runtime_model'] == f'opencode-go/{PREFERRED}'
    with pytest.raises(ValueError, match='zero-price'):
        exact.select_exact('opencode-free', 'opencode-go/paid-one', {})
    with pytest.raises(ValueError, match='cooldown'):
        exact.select_exact('opencode-free', f'opencode-go/{PREFERRED}',
                           {'opencode-free': [PREFERRED]})


def test_fast_runner_contract_and_no_model_or_provider_fallback():
    dispatch = (ROOT.parent / '.github/workflows/fixed-model-factory-dispatch.yml').read_text()
    entry = (ROOT.parent / '.github/workflows/free-model-factory-entry.yml').read_text()
    runner = (ROOT.parent / '.github/workflows/free-model-factory-run.yml').read_text()
    worker = (ROOT / '.github/scripts/free-model-factory-worker.sh').read_text()
    assert 'dispatch-next' in dispatch
    assert '--provider "$REQUESTED_PROVIDER" --model "$REQUESTED_MODEL"' in dispatch
    assert 'provider: ${{ inputs.provider }}' in entry
    assert 'FACTORY_EXACT_SOURCE: ${{ inputs.provider ||' in runner
    assert 'factory_manual_model.py' in runner
    assert 'Operator-pinned provider differs from leased worker' in runner
    assert 'FACTORY_EXACT_SOURCE' in worker
    assert 'No eligible provider slot currently has executable work' in (
        ROOT / '.github/scripts/factory-work-controller.py'
    ).read_text()


def test_force_slot_bypasses_advisory_health_but_keeps_existing_slot_and_target(monkeypatch):
    """Owner can direct a registered slot to a chosen task without fake work."""
    import factory_completion_controller as completion
    import factory_worker_slots as worker_slots
    spec = importlib.util.spec_from_file_location(
        'factory_work_controller_forced', ROOT / '.github/scripts/factory-work-controller.py'
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(worker_slots, 'slots', lambda: [
        SimpleNamespace(source='opencode-free', index=0, worker='1000000000001'),
        SimpleNamespace(source='opencode-free', index=1, worker='1000000000002'),
        SimpleNamespace(source='nvidia', index=0, worker='1000000000101'),
    ])
    monkeypatch.setattr(completion, 'durable_factory_state',
                        lambda: pytest.fail('force-slot must not read historical health'))
    monkeypatch.setattr(controller, 'assign', lambda worker, **kwargs: (
        calls.append((worker, kwargs)) or SimpleNamespace(
            kind='issue', number=5123, stage='', conflicted=False
        )
    ))
    monkeypatch.setattr(controller, 'assignment_context', lambda candidate, worker: {
        'worker': worker, 'kind': candidate.kind, 'number': candidate.number,
    })
    launched = []
    calls = []
    monkeypatch.setattr(controller, 'worker_skip_reason', lambda worker, **_kw: None)
    monkeypatch.setattr(controller, 'run_gh', lambda argv: launched.append(argv))
    outcome = controller.dispatch_next(
        'opencode-go', 'opencode-go/hy4-flash-free',
        force_slot=True, slot_index=2, target='issue:5123',
    )
    assert outcome['status'] == 'dispatched'
    assert outcome['worker'] == '1000000000002'
    assert outcome['forced'] is True
    # Owner decision (2026-11-21): a forced explicit target is an operator
    # override too, not normal targeted scheduling.
    assert calls == [('1000000000002', {
        'target': ('issue', 5123), 'requested_action': 'advance', 'operator_force': True,
        'named': True,
    })]
    assert 'model=opencode-go/hy4-flash-free' in launched[0]
    assert 'provider=opencode-free' in launched[0]


def test_force_slot_deferred_does_not_create_or_steal_a_claim(monkeypatch):
    import factory_worker_slots as worker_slots
    spec = importlib.util.spec_from_file_location(
        'factory_work_controller_force_deferral', ROOT / '.github/scripts/factory-work-controller.py'
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(worker_slots, 'slots', lambda: [
        SimpleNamespace(source='opencode-free', index=0, worker='1000000000001'),
    ])
    monkeypatch.setattr(controller, 'assign',
                        lambda *args, **kwargs: pytest.fail('a running worker must not be reassigned'))
    monkeypatch.setattr(controller, 'worker_skip_reason',
                        lambda worker, **_kw: f'Factory {worker} already has an active lease')
    monkeypatch.setattr(controller, 'run_gh',
                        lambda argv: pytest.fail('no worker should be started'))
    outcome = controller.dispatch_next('opencode-go', force_slot=True, slot_index=1)
    assert outcome['status'] == 'deferred'
    assert outcome['reason'] == 'All 1 opencode-go workers are running right now.'
    # Explicit operator slots may go past configured capacity, never past
    # the stable identity space.
    with pytest.raises(ValueError, match='does not exist'):
        controller.dispatch_next('opencode-go', force_slot=True, slot_index=100)
    with pytest.raises(ValueError, match='requires --force-slot'):
        controller.dispatch_next('opencode-go', slot_index=1)


def test_conflicted_pr_without_published_required_checks_can_enter_repair(monkeypatch):
    """GitHub missing-CI deadlock must not block a real conflict repair."""
    spec = importlib.util.spec_from_file_location(
        'factory_work_controller_conflict_check',
        ROOT / '.github/scripts/factory-work-controller.py',
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    candidate = SimpleNamespace(
        kind='pr', number=5575, stage='factory:changes-requested', conflicted=True,
    )
    monkeypatch.setattr(controller, 'target_json', lambda _: {
        'labels': [{'name': 'factory:changes-requested'}, {'name': 'factory:merge-conflict'}],
    })
    live = {
        'state': 'open', 'mergeable': False, 'mergeable_state': 'dirty',
        'head': {'sha': 'a' * 40},
    }
    monkeypatch.setattr(controller, 'live_pr_state', lambda _: live)
    required = {'output': ''}  # gh pr checks can emit an error/no stdout for no CI.
    monkeypatch.setattr(controller, 'run_gh', lambda *args, **kwargs: required['output'])
    check_runs = {'total_count': 2, 'check_runs': [
        {'name': 'Greptile Review', 'status': 'completed'},
        {'name': 'CodeRabbit', 'status': 'completed'},
    ]}
    check_calls = []

    def fake_check_runs(args):
        check_calls.append(args)
        return check_runs
    monkeypatch.setattr(controller, 'gh_json', fake_check_runs)

    assert controller.required_checks_outcome(5575) == 'waiting'
    assert controller.candidate_is_live_executable(candidate) is True
    assert check_calls[-1][-1].endswith('/commits/' + 'a' * 40 + '/check-runs?per_page=100')

    # Neither pending checks, an already-published CI Summary, nor missing API
    # evidence can be used to bypass the check gate.
    check_runs['check_runs'][0]['status'] = 'in_progress'
    assert controller.candidate_is_live_executable(candidate) is False
    check_runs['check_runs'][0]['status'] = 'completed'
    # #182: CI that already ran on the conflicted head (GitHub then reports no
    # required checks) settles nothing the conflict repair waits on.
    check_runs['check_runs'].append({'name': 'CI Summary', 'status': 'completed'})
    check_runs['total_count'] = 3
    assert controller.candidate_is_live_executable(candidate) is True
    check_runs['check_runs'][-1]['status'] = 'in_progress'
    assert controller.candidate_is_live_executable(candidate) is False
    check_runs['check_runs'].pop()
    check_runs['total_count'] = 3  # pagination or partial API response
    assert controller.candidate_is_live_executable(candidate) is False
    check_runs['total_count'] = 2

    live['mergeable'] = True
    live['mergeable_state'] = 'clean'
    assert controller.candidate_is_live_executable(candidate) is False
    live['mergeable'] = False
    live['mergeable_state'] = 'dirty'
    candidate.conflicted = False
    assert controller.candidate_is_live_executable(candidate) is False
    candidate.conflicted = True

    candidate.stage = 'factory:ci'
    monkeypatch.setattr(controller, 'target_json', lambda _: {
        'labels': [{'name': 'factory:ci'}, {'name': 'factory:merge-conflict'}],
    })
    assert controller.candidate_is_live_executable(candidate) is False
    candidate.stage = 'factory:changes-requested'
    monkeypatch.setattr(controller, 'target_json', lambda _: {
        'labels': [{'name': 'factory:ready'}],
    })
    assert controller.candidate_is_live_executable(candidate) is False


def test_conflict_repair_unknown_check_snapshot_fails_closed(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'factory_work_controller_conflict_missing',
        ROOT / '.github/scripts/factory-work-controller.py',
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(controller, 'live_pr_state', lambda _: {
        'state': 'open', 'mergeable': False, 'mergeable_state': 'dirty',
        'head': {'sha': 'a' * 40},
    })
    monkeypatch.setattr(controller, 'gh_json', lambda _: (_ for _ in ()).throw(RuntimeError('API down')))
    assert controller.conflicted_head_without_published_ci(5575) is False


def test_worker_preflight_preserves_live_conflict_state_from_leased_pr(monkeypatch):
    """Dispatch and worker preflight must agree on conflicted-head eligibility."""
    spec = importlib.util.spec_from_file_location(
        'factory_work_controller_conflict_preflight',
        ROOT / '.github/scripts/factory-work-controller.py',
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    worker = '1327811785100'
    head = 'a' * 40
    branch = 'factory/work-checkpoints/issue-4737/beam-free/43742763406'
    ctx = {'kind': 'pr', 'number': 5575, 'worker': worker,
           'claim_id': 'exact-lease', 'stage': 'factory:changes-requested',
           'head': head, 'branch': branch}
    monkeypatch.setattr(controller, '_active_claim', lambda *_args:
                        ('exact-lease', [5575]))
    labels = [{'name': 'factory:' + worker},
              {'name': 'factory:changes-requested'},
              {'name': 'factory:merge-conflict'}]
    monkeypatch.setattr(controller, 'target_json', lambda _: {
        'state': 'open', 'labels': labels,
    })
    live = {'state': 'open', 'head': {'sha': head, 'ref': branch},
            'labels': labels, 'mergeable': False, 'mergeable_state': 'dirty'}
    monkeypatch.setattr(controller, 'live_pr_state', lambda _: live)
    inspected = []
    def gate(candidate):
        inspected.append(candidate)
        return candidate.conflicted is True
    monkeypatch.setattr(controller, 'candidate_is_live_executable', gate)

    assert controller.validate_assignment(worker, json.dumps(ctx)) == ctx
    assert inspected[-1].conflicted is True
    live['mergeable'] = True
    live['mergeable_state'] = 'clean'
    with pytest.raises(ValueError, match='waiting or no longer executable'):
        controller.validate_assignment(worker, json.dumps(ctx))
    assert inspected[-1].conflicted is False
    live['mergeable'] = False
    live['mergeable_state'] = 'dirty'
    live['head']['sha'] = 'b' * 40
    with pytest.raises(ValueError, match='head moved'):
        controller.validate_assignment(worker, json.dumps(ctx))


def test_operator_force_reaches_real_work_without_soft_intake_or_retry_filters(monkeypatch):
    """One operator-forced slot should see the existing backlog, not a fake task."""
    import factory_state_operations
    spec = importlib.util.spec_from_file_location(
        'factory_work_controller_override_policy',
        ROOT / '.github/scripts/factory-work-controller.py',
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    candidate = controller.Candidate(
        'issue', 5592, 1, 4, '2026-11-21T11:00:00Z',
    )
    issues = [{'number': 5592, 'state': 'OPEN',
               'labels': [{'name': 'factory:unowned'}, {'name': 'user-reported'}]}]
    monkeypatch.setattr(controller, 'reconcile_stale_leases', lambda: [])
    monkeypatch.setattr(controller, 'reconcile_contradictory_labels', lambda: [])
    monkeypatch.setattr(controller, 'list_issues', lambda: issues)
    monkeypatch.setattr(controller, 'list_prs', lambda: [])
    monkeypatch.setattr(controller, 'load_no_diff_attempt_records', lambda: [])
    monkeypatch.setenv('FACTORY_JEV_RANKING_FILE', '')
    observed = {}
    def build(_issues, _prs, **kwargs):
        observed['backpressure_override'] = kwargs['include_backpressured_issues']
        return [candidate]
    def intake(_issues, _prs, *, operator_force=False):
        observed['portable_override'] = operator_force
        return {5592}
    monkeypatch.setattr(controller, 'build_candidates', build)
    monkeypatch.setattr(controller, 'rotisserie_issue_intake', intake)
    monkeypatch.setattr(controller, 'resolve_review_head_contributors',
                        lambda rows, _prs: rows)
    monkeypatch.setattr(controller, 'order_candidates_for_worker',
                        lambda rows, _worker: rows)
    monkeypatch.setattr(controller, 'candidate_is_live_executable', lambda _: True)
    monkeypatch.setattr(controller, 'issue_excludes_worker_on_strike_retry',
                        lambda *_: False)
    monkeypatch.setattr(controller, 'assign_candidate', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        factory_state_operations, 'cooling_prs',
        lambda *_args, **_kwargs:
        pytest.fail('operator force must bypass only advisory retry cooldown'),
    )
    chosen = controller.assign_from_pool('6', operator_force=True)
    assert chosen is candidate
    assert observed == {'backpressure_override': True, 'portable_override': True}


def test_operator_force_dispatch_passes_owner_override_into_assignment(monkeypatch):
    """The --force-slot flow is more than ignoring historical slot health."""
    import factory_worker_slots as worker_slots
    spec = importlib.util.spec_from_file_location(
        'factory_work_controller_force_autorun',
        ROOT / '.github/scripts/factory-work-controller.py',
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, controller)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(worker_slots, 'slots', lambda: [
        SimpleNamespace(source='opencode-free', index=0, worker='1000000000001'),
    ])
    calls = []
    monkeypatch.setattr(controller, 'assign',
                        lambda worker, **kwargs: calls.append((worker, kwargs)) or None)
    monkeypatch.setattr(controller, 'worker_skip_reason', lambda worker, **_kw: None)
    result = controller.dispatch_next(
        'opencode-go', 'opencode-go/hy4-flash-free', force_slot=True,
    )
    assert result['status'] == 'deferred'
    assert result['reason'] == 'Nothing is waiting: every open item is finished, claimed or blocked.'
    # The shared pool is identical for every identity: one look is enough.
    assert calls == [('1000000000001', {'operator_force': True, 'named': True})]
