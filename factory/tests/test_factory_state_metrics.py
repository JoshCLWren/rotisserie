"""Measurement output must never persist query text, arguments or secrets."""
from pathlib import Path
import importlib.util
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / '.github/scripts/factory_state_metrics.py'
SPEC = importlib.util.spec_from_file_location('factory_state_metrics_test', SCRIPT)
assert SPEC and SPEC.loader
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def test_counts_are_local_and_exclude_sql_parameters_and_credentials(tmp_path, monkeypatch):
    path = tmp_path / 'metrics'
    monkeypatch.setenv('FACTORY_STATE_METRICS_FILE', str(path))
    for sql in ['SELECT secret_value FROM private_table', 'UPDATE private_table SET secret=$1',
                'CREATE TABLE private_table(secret text)',
                'WITH changed AS (DELETE FROM private_table RETURNING id) SELECT count(*) FROM changed']:
        metrics.record_query(SimpleNamespace(query=sql, args=['password-sentinel'], elapsed=0.002, exception=None))
    text = path.read_text()
    for secret in ['secret_value', 'private_table', 'password-sentinel', 'SELECT', 'UPDATE']:
        assert secret not in text
    assert metrics.summarize(path) == {'read': 1, 'write': 2, 'ddl': 1, 'control': 0, 'failed': 0, 'milliseconds': 8.0}


def test_metrics_failure_cannot_change_query_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv('FACTORY_STATE_METRICS_FILE', str(tmp_path / 'missing' / 'metrics'))
    metrics.record_query(SimpleNamespace(query='SELECT 1', elapsed=0, exception=None))


async def test_connection_wrapper_preserves_driver_arguments_and_adds_only_opt_in_logger(monkeypatch, tmp_path):
    import sys

    calls = []
    loggers = []
    connection = SimpleNamespace(add_query_logger=loggers.append)
    async def connect(**kwargs):
        calls.append(kwargs)
        return connection
    monkeypatch.setitem(sys.modules, 'asyncpg', SimpleNamespace(connect=connect))
    monkeypatch.delenv('FACTORY_STATE_METRICS_FILE', raising=False)
    assert await metrics.connect(dsn='private-sentinel', timeout=8, command_timeout=15) is connection
    assert loggers == []
    monkeypatch.setenv('FACTORY_STATE_METRICS_FILE', str(tmp_path / 'metrics'))
    assert await metrics.connect(dsn='private-sentinel', timeout=8, command_timeout=15) is connection
    assert loggers == [metrics.record_query]
    assert calls == [{'dsn': 'private-sentinel', 'timeout': 8, 'command_timeout': 15}] * 2


def test_explicit_measurement_cannot_be_evicted_by_unrelated_ingress():
    import yaml

    workflow = yaml.safe_load((SCRIPT.parents[1] / 'workflows/fixed-model-factory-dispatch.yml').read_text())
    group = workflow['concurrency']['group']
    # A measurement run gets its own run-unique group; targeted work its own
    # target/action group. Neither shares the ingress queue.
    assert "inputs.measure && format('factory-measure-{0}', github.run_id)" in group
    assert "inputs.target && format('factory-target-{0}-{1}', inputs.target" in group
    assert group.endswith("|| 'fixed-model-factory-dispatch-ingress' }}")
    assert workflow['concurrency']['cancel-in-progress'] is False
    assert workflow['jobs']['dispatch']['concurrency']['group'] == 'fixed-model-factory-dispatch'
