"""Per-run scheduling memory reduces discovery without caching GitHub authority."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / '.github/scripts'
sys.path.insert(0, str(SCRIPTS))
discovery = importlib.import_module('factory_discovery')


def test_discovery_fetches_once_per_job_and_never_shares_between_runs(tmp_path, monkeypatch):
    reads = []
    def fetch():
        reads.append(True)
        return [{'number': len(reads)}]
    monkeypatch.setenv('FACTORY_STATE_DSN_FILE', '/must-not-read-neon')
    monkeypatch.setenv('FACTORY_DISCOVERY_DIR', str(tmp_path / 'run-one'))
    assert discovery.discover('owner/repo', 'issue', fetch) == [{'number': 1}]
    assert discovery.discover('owner/repo', 'issue', fetch) == [{'number': 1}]
    monkeypatch.setenv('FACTORY_DISCOVERY_DIR', str(tmp_path / 'run-two'))
    assert discovery.discover('owner/repo', 'issue', fetch) == [{'number': 2}]
    assert len(reads) == 2


def test_empty_inventory_and_repository_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv('FACTORY_DISCOVERY_DIR', str(tmp_path))
    assert discovery.discover('owner/repo', 'pr', lambda: []) == []
    assert discovery.discover('owner/repo', 'pr', lambda: pytest.fail('empty snapshot ignored')) == []
    assert discovery.read_snapshot('other/repo', 'pr') is None


def test_own_label_writes_update_scheduling_memory(tmp_path, monkeypatch):
    monkeypatch.setenv('FACTORY_DISCOVERY_DIR', str(tmp_path))
    discovery.discover('owner/repo', 'pr', lambda: [{'number': 42, 'labels': []}])
    labels = [{'name': 'factory:unowned'}]
    discovery.update_labels('owner/repo', 'pr', 42, labels)
    snapshot = discovery.read_snapshot('owner/repo', 'pr')
    assert snapshot is not None
    assert snapshot[0]['labels'] == labels


@pytest.fixture
def controller():
    spec = importlib.util.spec_from_file_location('targeted_discovery_controller', SCRIPTS / 'factory-work-controller.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pr_event_reads_only_pr_linked_issue_and_cutover_gate(controller, monkeypatch, tmp_path):
    monkeypatch.setenv('FACTORY_DISCOVERY_TARGET', 'pr:42')
    monkeypatch.setenv('FACTORY_DISCOVERY_DIR', str(tmp_path))
    calls = []
    epic = controller.eligibility.RUST_CUTOVER_EPIC
    def pr(number):
        calls.append(('pr', number))
        return {'number': number, 'body': 'Closes #41', 'title': 'Fix issue',
                'head': {'sha': 'a' * 40, 'ref': 'local/repair', 'repo': {'full_name': 'owner/repo'}},
                'base': {'repo': {'full_name': 'owner/repo'}}, 'labels': []}
    monkeypatch.setattr(controller, 'live_pr_state', pr)
    monkeypatch.setattr(controller, 'target_json', lambda number: calls.append(('issue', number)) or {'number': number, 'state': 'open'})
    monkeypatch.setattr(controller, 'gh_json', lambda *_: pytest.fail('global inventory discovery'))
    assert controller.list_prs()[0]['number'] == 42
    assert [row['number'] for row in controller.list_issues()] == [41, epic]
    assert calls == [('pr', 42), ('issue', 41), ('issue', epic)]


def test_targeted_issue_snapshot_retains_rust_freeze(controller, monkeypatch):
    epic = controller.eligibility.RUST_CUTOVER_EPIC
    issue_number = epic + 1
    monkeypatch.setenv('FACTORY_DISCOVERY_TARGET', f'issue:{issue_number}')
    rows = {
        issue_number: {'number': issue_number, 'state': 'open',
                       'body': 'Product fix', 'labels': [{'name': 'factory:unowned'}]},
        epic: {'number': epic, 'state': 'open', 'body': 'Tracking epic',
               'labels': [{'name': 'epic'}]},
    }
    monkeypatch.setattr(controller, 'target_json', lambda n: rows[n])
    monkeypatch.setattr(controller, 'gh_json', lambda *_: pytest.fail('global inventory discovery'))
    selected = controller.list_issues()
    assert [row['number'] for row in selected] == [issue_number, epic]
    assert controller.build_candidates(selected, []) == []
    labels = rows[issue_number]['labels']
    assert isinstance(labels, list)
    labels.append({'name': 'rust'})
    rust_candidates = controller.build_candidates(controller.list_issues(), [])
    assert [(candidate.kind, candidate.number) for candidate in rust_candidates] == [('issue', issue_number)]


def test_worker_ownership_check_is_independent_of_narrow_candidate_view(controller, monkeypatch):
    observed: list[list[str]] = []
    monkeypatch.setattr(controller, 'list_issues', lambda: pytest.fail('candidate inventory is not ownership'))
    monkeypatch.setattr(controller, 'list_prs', lambda: pytest.fail('candidate inventory is not ownership'))

    def _record_ownership_observation(args: list[str]) -> list[dict[str, int]]:
        observed.append(args)
        return [{'number': 99}]

    monkeypatch.setattr(controller, 'gh_json', _record_ownership_observation)
    assert controller.worker_has_active_lease('54') is True
    assert observed == [['api', 'repos/JoshCLWren/comic-pile/issues?state=open&labels=factory%3A54&per_page=1']]


def test_obsolete_neon_discovery_producers_are_removed():
    for path in ('.github/scripts/factory_gh_read_through.py', '.github/scripts/factory_state_projection.py',
                 '.github/workflows/factory-target-projections.yml'):
        assert not (ROOT / path).exists()
    shim = (SCRIPTS / 'factory-gh-rest-shim.sh').read_text()
    assert 'FACTORY_STATE_DSN_FILE' not in shim
    assert 'factory_gh_read_through' not in shim
