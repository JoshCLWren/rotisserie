"""Model-first factory selection uses existing leases and live catalog membership."""
from __future__ import annotations

import importlib.util
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
    monkeypatch.setattr(controller, 'assign', lambda worker: (
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
    dispatch = (ROOT / '.github/workflows/fixed-model-factory-dispatch.yml').read_text()
    entry = (ROOT / '.github/workflows/free-model-factory-entry.yml').read_text()
    runner = (ROOT / '.github/workflows/free-model-factory-run.yml').read_text()
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
    monkeypatch.setattr(controller, 'run_gh', lambda argv: launched.append(argv))
    outcome = controller.dispatch_next(
        'opencode-go', 'opencode-go/hy4-flash-free',
        force_slot=True, slot_index=2, target='issue:5123',
    )
    assert outcome['status'] == 'dispatched'
    assert outcome['worker'] == '1000000000002'
    assert outcome['forced'] is True
    assert calls == [('1000000000002', {'target': ('issue', 5123)})]
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
    monkeypatch.setattr(controller, 'assign', lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, 'worker_skip_reason', lambda worker: 'active Neon lease')
    monkeypatch.setattr(controller, 'run_gh',
                        lambda argv: pytest.fail('no worker should be started'))
    outcome = controller.dispatch_next('opencode-go', force_slot=True, slot_index=1)
    assert outcome['status'] == 'deferred'
    assert 'active Neon lease' in outcome['details']
    with pytest.raises(ValueError, match='does not exist'):
        controller.dispatch_next('opencode-go', force_slot=True, slot_index=99)
    with pytest.raises(ValueError, match='requires --force-slot'):
        controller.dispatch_next('opencode-go', slot_index=1)


def test_conflicted_pr_without_published_required_checks_can_enter_repair(monkeypatch):
    """A conflict is not blocked forever by CI that cannot publish on its head."""
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
    live = {'state': 'open', 'mergeable': False, 'mergeable_state': 'dirty'}
    monkeypatch.setattr(controller, 'live_pr_state', lambda _: live)
    responses = {'checks': '[]'}
    monkeypatch.setattr(controller, 'run_gh', lambda *args, **kwargs: responses['checks'])

    # Missing is distinguished only for a known conflicted head.
    assert controller.required_checks_outcome(5575) == 'waiting'
    assert controller.required_checks_outcome(5575, distinguish_absent=True) == 'absent'
    assert controller.candidate_is_live_executable(candidate) is True

    # No blanket bypass of active or malformed required check evidence.
    for payload in ('[{"state":"PENDING"}]', '', '{broken'):
        responses['checks'] = payload
        assert controller.candidate_is_live_executable(candidate) is False

    responses['checks'] = '[]'
    # A stale conflict snapshot may not authorize work on a clean PR head.
    live['mergeable'] = True
    live['mergeable_state'] = 'clean'
    assert controller.candidate_is_live_executable(candidate) is False
    live['mergeable'] = False
    live['mergeable_state'] = 'dirty'

    # CI-stage and non-conflicted PRs still wait for required checks.
    candidate.stage = 'factory:ci'
    assert controller.candidate_is_live_executable(candidate) is False
    candidate.stage = 'factory:changes-requested'
    candidate.conflicted = False
    assert controller.candidate_is_live_executable(candidate) is False

    # An explicit ready stage remains a hard stop even if the PR is conflicted.
    candidate.conflicted = True
    monkeypatch.setattr(controller, 'target_json', lambda _: {
        'labels': [{'name': 'factory:ready'}],
    })
    assert controller.candidate_is_live_executable(candidate) is False
