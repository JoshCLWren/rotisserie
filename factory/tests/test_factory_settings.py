"""Operator settings resolve env -> repository variable -> default, safely."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/scripts'))

import factory_settings as settings  # noqa: E402
import factory_work_policy as policy  # noqa: E402
import factory_worker_slots as worker_slots  # noqa: E402


def test_resolution_order_and_invalid_values_fall_back(monkeypatch):
    monkeypatch.delenv('FACTORY_PR_WIP_LIMIT', raising=False)
    monkeypatch.delenv(settings.REPO_VARS_ENV, raising=False)
    assert settings.resolve('FACTORY_PR_WIP_LIMIT') == (8, 'default')
    monkeypatch.setenv(settings.REPO_VARS_ENV, json.dumps({'FACTORY_PR_WIP_LIMIT': '15'}))
    assert settings.resolve('FACTORY_PR_WIP_LIMIT') == (15, 'repository variable')
    monkeypatch.setenv('FACTORY_PR_WIP_LIMIT', '3')
    assert settings.resolve('FACTORY_PR_WIP_LIMIT') == (3, 'environment')
    monkeypatch.setenv('FACTORY_PR_WIP_LIMIT', 'lots')
    value, origin = settings.resolve('FACTORY_PR_WIP_LIMIT')
    assert value == 8 and origin.startswith('default')
    monkeypatch.setenv(settings.REPO_VARS_ENV, 'not json')
    monkeypatch.delenv('FACTORY_PR_WIP_LIMIT')
    assert settings.resolve('FACTORY_PR_WIP_LIMIT') == (8, 'default')


def test_registry_defaults_match_every_call_site_default():
    """One source of truth: call-site fallbacks may not drift from the registry."""
    sources = '\n'.join(path.read_text() for path in (ROOT / '.github/scripts').glob('*.py'))
    for name, default in re.findall(r"env_positive_int\(\s*['\"]([A-Z_]+)['\"],\s*(\d+)\)", sources):
        if name in settings.BY_NAME:
            assert settings.BY_NAME[name].default == int(default), name


def test_workers_per_provider_reads_the_repository_variable(monkeypatch):
    monkeypatch.delenv('FACTORY_SLOTS_PER_PROVIDER', raising=False)
    monkeypatch.setenv(settings.REPO_VARS_ENV, json.dumps({'FACTORY_SLOTS_PER_PROVIDER': '3'}))
    assert worker_slots.slot_count() == 3
    # Explicit test mappings never read ambient repository variables.
    assert worker_slots.slot_count({}) == 12


def test_priority_order_changes_pick_up_order_not_semantic_lanes(monkeypatch):
    infra = policy.Candidate('issue', 1, policy.provenance_lane({'infrastructure'} & policy.INFRA_LABELS or {next(iter(policy.INFRA_LABELS))}), 0, '2026-01-01T00:00:00Z')
    normal = policy.Candidate('issue', 2, policy.provenance_lane(set()), 0, '2026-01-01T00:00:00Z')
    assert (infra.lane, normal.lane) == (5, 3)
    monkeypatch.delenv('FACTORY_PRIORITY_ORDER', raising=False)
    assert sorted([infra, normal], key=lambda c: c.sort_key())[0] is normal
    monkeypatch.setenv('FACTORY_PRIORITY_ORDER', 'infrastructure,main-breakage')
    assert sorted([infra, normal], key=lambda c: c.sort_key())[0] is infra
    assert infra.lane == 5  # meaning unchanged for policy that reads lanes


@pytest.mark.parametrize('raw, message', [
    ('normal,bogus', 'unknown priority'),
    ('normal,normal', 'only once'),
])
def test_priority_order_validation(raw, message):
    with pytest.raises(ValueError, match=message):
        settings.validate(settings.BY_NAME['FACTORY_PRIORITY_ORDER'], raw)


def test_partial_priority_order_keeps_remaining_categories_in_default_order():
    value = str(settings.validate(settings.BY_NAME['FACTORY_PRIORITY_ORDER'], 'e2e'))
    assert value.split(',')[0] == 'e2e'
    assert sorted(value.split(',')) == sorted(settings.PRIORITY_CATEGORIES)


def test_every_factory_workflow_receives_exactly_the_registered_settings():
    # Only registered settings, never toJSON(vars): ComicPile logs are public.
    expected = settings.workflow_env_value()
    for workflow in (ROOT.parent / '.github/workflows').glob('*.yml'):
        if workflow.name in ('ci.yml', 'canary-merge.yml'):
            continue
        text = workflow.read_text()
        assert 'toJSON(vars)' not in text, workflow.name
        assert f'FACTORY_REPO_VARS: >-\n    {expected}\n' in text, workflow.name


def test_unset_repository_variables_arrive_empty_and_mean_default(monkeypatch):
    monkeypatch.delenv('FACTORY_PR_WIP_LIMIT', raising=False)
    monkeypatch.setenv(settings.REPO_VARS_ENV, json.dumps({'FACTORY_PR_WIP_LIMIT': ''}))
    assert settings.resolve('FACTORY_PR_WIP_LIMIT') == (8, 'default')


def test_settings_module_ships_in_the_adopter_overlay():
    overlay = (ROOT / 'install/overlay-paths.txt').read_text().splitlines()
    assert '.github/scripts/factory_settings.py' in overlay


def test_unset_workers_per_provider_variable_means_default(monkeypatch):
    """Regression: workflows deliver unset variables as "" (live failure 2026-11-22)."""
    monkeypatch.delenv('FACTORY_SLOTS_PER_PROVIDER', raising=False)
    monkeypatch.setenv(settings.REPO_VARS_ENV, json.dumps({'FACTORY_SLOTS_PER_PROVIDER': ''}))
    assert worker_slots.slot_count() == 12
    assert len(worker_slots.slots()) == 12 * len(worker_slots.sources())


def test_every_reader_accepts_the_exact_workflow_payload_when_nothing_is_set(monkeypatch):
    """Render the real workflow value with every variable unset, as GitHub does."""
    import re as regex
    payload = regex.sub(r'\$\{\{ vars\.[A-Z_]+ \}\}', '', settings.workflow_env_value())
    monkeypatch.setenv(settings.REPO_VARS_ENV, payload)
    for name in settings.BY_NAME:
        monkeypatch.delenv(name, raising=False)
        assert settings.resolve(name)[1] == 'default'
    assert worker_slots.slot_count() == 12
    assert policy.env_positive_int('FACTORY_PR_WIP_LIMIT', 8) == 8
