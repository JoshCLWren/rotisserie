"""Delivery outcomes, squash attribution and bounded immutable metadata reuse."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'factory_delivery_funnel', Path('.github/scripts/factory_delivery_funnel.py'),
)
assert SPEC and SPEC.loader
funnel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = funnel
SPEC.loader.exec_module(funnel)
PINS = [
    {'worker': '54', 'model': 'glm-4.5-flash', 'display_name': 'Z.AI GLM 4.5 Flash'},
    {'worker': '46', 'model': 'kilo-auto/free', 'display_name': 'Kilo Auto Free · Forge'},
]
NOW = datetime(2026, 11, 7, tzinfo=UTC)
SHA = 'a' * 40
ADVANCES = ['factory: advance #7 with Z.AI GLM 4.5 Flash',
            'factory: advance PR #8 with Kilo Auto Free · Forge']


def test_squash_bullets_and_served_models_have_distinct_credit() -> None:
    """Attribution uses advance lines rather than arbitrary Model fields or tool prose."""
    counts = funnel.commit_credit([
        '* ' + ADVANCES[0] + '\n\n* ' + ADVANCES[1] + ' (served: vendor/a,vendor/b)',
        'Model: ignored\nprose mentions factory: advance #7 with forged',
    ], PINS)
    assert counts[('54', 'glm-4.5-flash')] == 1
    assert counts[('46', 'vendor/a')] == 0.5
    assert counts[('46', 'vendor/b')] == 0.5
    assert sum(counts.values()) == 2


def test_event_windows_closed_outcomes_retired_workers_and_cache(tmp_path: Path) -> None:
    """Separate event windows and normalized credit cannot duplicate squash copies."""
    prs = [
        {'number': 1, 'head': {'ref': 'factory/54-7-z-ai'}, 'labels': [],
         'body': 'Model: glm-4.5-flash', 'created_at': '2026-10-01T00:00:00Z',
         'updated_at': '2026-11-06T00:00:00Z', 'merged_at': '2026-11-06T00:00:00Z',
         'closed_at': '2026-11-06T00:00:00Z', 'merge_commit_sha': SHA},
        {'number': 2, 'head': {'ref': 'factory/55-9-ollama'}, 'labels': [],
         'body': 'Model: retired-model', 'created_at': '2026-10-15T00:00:00Z',
         'updated_at': '2026-10-20T00:00:00Z', 'closed_at': '2026-10-20T00:00:00Z'},
        {'number': 3, 'head': {'ref': 'human'}, 'labels': [],
         'updated_at': '2026-11-06T00:00:00Z'},
    ]
    calls: list[str] = []

    def fetch(endpoint: str) -> object:
        calls.append(endpoint)
        if endpoint.startswith('pulls?'):
            return prs
        if endpoint.startswith('pulls/1/commits?'):
            return [{'commit': {'message': message}} for message in ADVANCES]
        if endpoint == f'commits/{SHA}':
            return {'commit': {'message': 'Squashed\n' + '\n'.join('* ' + m for m in ADVANCES)}}
        raise AssertionError(endpoint)

    cache = tmp_path / 'cache.json'
    rows = funnel.collect_delivery_funnel(fetch, PINS, now=NOW, cache_path=cache)
    by_key = {(row['group'], row['name'], row['days']): row for row in rows}
    assert by_key[('worker', '54', 14)]['merged'] == 1
    assert by_key[('worker', '54', 14)]['opened'] == 0
    assert by_key[('worker', '54', 14)]['closed'] == 0
    assert by_key[('worker', '54', 14)]['commit_share'] == 0.5
    assert by_key[('model', 'kilo-auto/free', 14)]['commit_share'] == 0.5
    assert by_key[('worker', '55', 30)]['closed'] == 1
    assert by_key[('worker', '55', 14)]['closed'] == 0
    calls.clear()
    assert funnel.collect_delivery_funnel(fetch, PINS, now=NOW, cache_path=cache) == rows
    assert len(calls) == 1  # No repeat commit reads on a five-minute refresh.


def test_original_commit_pagination_is_complete(tmp_path: Path) -> None:
    """More than one hundred advances cannot disappear behind a first-page limit."""
    calls: list[str] = []

    def fetch(endpoint: str) -> object:
        calls.append(endpoint)
        if endpoint.endswith('page=1'):
            return [{'commit': {'message': ADVANCES[0]}}] * 100
        return [{'commit': {'message': ADVANCES[1]}}]

    assert len(funnel.paginated(fetch, 'pulls/1/commits')) == 101
    assert len(calls) == 2


def test_invalid_api_history_is_not_a_zero_success_sample() -> None:
    """A malformed response fails closed for the dashboard's unavailable state."""
    with pytest.raises(ValueError, match='invalid funnel'):
        funnel.records({'message': 'API unavailable'})


def test_ambiguous_display_name_credits_unattributed_but_keeps_proven_model() -> None:
    """A display name shared by two workers cannot steal another worker's credit."""
    pins = [
        {'worker': '54', 'model': 'glm-4.5-flash', 'display_name': 'Shared Name'},
        {'worker': '55', 'model': 'glm-4.5-flash', 'display_name': 'Shared Name'},
    ]
    counts = funnel.commit_credit(['factory: advance #1 with Shared Name'], pins)
    assert counts[('unattributed', 'glm-4.5-flash')] == 1
    assert ('54', 'glm-4.5-flash') not in counts
    assert ('55', 'glm-4.5-flash') not in counts


def test_ambiguous_display_name_with_conflicting_models_falls_back_to_name() -> None:
    """When colliding rows disagree on the model, attribution falls back to the label."""
    pins = [
        {'worker': '54', 'model': 'model-a', 'display_name': 'Shared Name'},
        {'worker': '55', 'model': 'model-b', 'display_name': 'Shared Name'},
    ]
    counts = funnel.commit_credit(['factory: advance #1 with Shared Name'], pins)
    assert counts[('unattributed', 'Shared Name')] == 1


@pytest.mark.parametrize(
    'count',
    [0, -1, float('nan'), float('inf'), float('-inf'), 'oops', None],
)
def test_invalid_cached_count_is_not_a_zero_success_sample(
    tmp_path: Path, count: object,
) -> None:
    """Non-finite or non-positive cached counts fail closed for the dashboard."""
    prs = [
        {'number': 1, 'head': {'ref': 'factory/54-7-z-ai'}, 'labels': [],
         'body': 'Model: glm-4.5-flash', 'created_at': '2026-11-06T00:00:00Z',
         'updated_at': '2026-11-06T00:00:00Z', 'merged_at': '2026-11-06T00:00:00Z',
         'closed_at': '2026-11-06T00:00:00Z', 'merge_commit_sha': SHA},
    ]

    def fetch(endpoint: str) -> object:
        if endpoint.startswith('pulls?'):
            return prs
        raise AssertionError(endpoint)

    cache = tmp_path / 'cache.json'
    cache.write_text(json.dumps({
        'version': 1,
        'merges': {SHA: [{'worker': '54', 'model': 'glm-4.5-flash', 'count': count}]},
    }))
    with pytest.raises(ValueError, match='invalid funnel cache count'):
        funnel.collect_delivery_funnel(fetch, PINS, now=NOW, cache_path=cache)


def test_merged_pr_without_advance_commits_keeps_delivery_counts(tmp_path: Path) -> None:
    """Missing advance metadata means zero contributor credit, not a broken dashboard."""
    pr = {
        'number': 1, 'head': {'ref': 'factory/54-7-z-ai'}, 'labels': [],
        'body': 'Model: glm-4.5-flash', 'created_at': '2026-11-06T00:00:00Z',
        'updated_at': '2026-11-06T00:00:00Z', 'merged_at': '2026-11-06T00:00:00Z',
        'closed_at': '2026-11-06T00:00:00Z', 'merge_commit_sha': SHA,
    }

    def fetch(endpoint: str) -> object:
        if endpoint.startswith('pulls?'):
            return [pr]
        if endpoint.startswith('pulls/1/commits?'):
            return [{'commit': {'message': 'implementation with no factory advance'}}]
        if endpoint == f'commits/{SHA}':
            return {'commit': {'message': 'Merge ordinary implementation'}}
        raise AssertionError(endpoint)

    cache = tmp_path / 'cache.json'
    rows = funnel.collect_delivery_funnel(fetch, PINS, now=NOW, cache_path=cache)
    by_key = {(row['group'], row['name'], row['days']): row for row in rows}
    assert by_key[('worker', '54', 14)]['merged'] == 1
    assert by_key[('worker', '54', 14)]['commit_share'] == 0
    assert by_key[('model', 'glm-4.5-flash', 14)]['merged'] == 1
    assert funnel.collect_delivery_funnel(fetch, PINS, now=NOW, cache_path=cache) == rows
