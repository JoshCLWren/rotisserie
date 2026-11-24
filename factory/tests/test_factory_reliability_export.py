"""Reliability snapshot counts real outcomes per provider and model."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/scripts'))

import factory_state_operations as operations  # noqa: E402


def test_summary_counts_deliveries_idles_and_failure_reasons():
    rows = [
        {'window': '24h', 'source': 'opencode-free', 'model': 'hy4-flash-free',
         'event_type': 'worker_completed', 'delivered': True, 'outcome_class': 'success', 'n': 3},
        {'window': '24h', 'source': 'opencode-free', 'model': 'hy4-flash-free',
         'event_type': 'worker_failed', 'delivered': False, 'outcome_class': 'provider_quota', 'n': 2},
        {'window': '7d', 'source': 'opencode-free', 'model': 'hy4-flash-free',
         'event_type': 'worker_idle', 'delivered': False, 'outcome_class': 'no_work', 'n': 1},
        {'window': '7d', 'source': 'nvidia', 'model': None,
         'event_type': 'worker_failed', 'delivered': False, 'outcome_class': None, 'n': 1},
    ]
    health = [{'source': 'opencode-free', 'model': 'hy4-flash-free', 'status': 'healthy',
               'last_outcome': 'success', 'cooldown_until': None}]
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    report = operations.summarize_reliability(rows, health, now)
    hy4 = next(m for m in report['models'] if m['model'] == 'hy4-flash-free')
    assert hy4['windows']['24h'] == {'runs': 5, 'delivered': 3, 'completed': 3, 'idle': 0,
                                     'failed': 2, 'failures': {'provider_quota': 2}}
    assert hy4['windows']['7d']['idle'] == 1
    assert hy4['health']['status'] == 'healthy'
    unknown = next(m for m in report['models'] if m['source'] == 'nvidia')
    assert unknown['model'] == 'unclassified'
    assert unknown['windows']['7d']['failures'] == {'unknown_failure': 1}
    providers = {p['source']: p for p in report['providers']}
    assert providers['opencode-free']['windows']['24h']['delivered'] == 3
    assert report['generated_at'] == now.isoformat()


def test_status_pages_publish_the_snapshot():
    workflow = (ROOT.parent / '.github/workflows/factory-status-pages.yml').read_text()
    assert 'export-reliability > site/factory-stats.json.tmp' in workflow
    assert 'publish_snapshot site/factory-stats.json factory-stats.json' in workflow
