"""Opt-in, credential-free query counts for trusted factory measurements."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


def record_query(record: Any) -> None:
    """Record only operation type, elapsed time and failure; never SQL or args."""
    destination = os.environ.get('FACTORY_STATE_METRICS_FILE')
    if not destination:
        return
    statement = record.query.lstrip()
    verb = statement.split(None, 1)[0].upper() if statement else 'UNKNOWN'
    kind = ('control' if verb in {'BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'RELEASE'} else
            'ddl' if verb in {'CREATE', 'ALTER', 'DROP'} else
            'write' if verb in {'INSERT', 'UPDATE', 'DELETE'} or (
                verb == 'WITH' and re.search(r'\b(?:INSERT|UPDATE|DELETE)\b', statement, re.I)
            ) else 'read')
    item = {'kind': kind, 'milliseconds': round(float(record.elapsed) * 1000, 3),
            'failed': record.exception is not None}
    try:
        with Path(destination).open('a') as output:
            output.write(json.dumps(item) + '\n')
    except OSError:
        # Diagnostics must never change lease/transaction behavior.
        pass


async def connect(*, dsn: str, timeout: int, command_timeout: int) -> Any:
    """Keep asyncpg behavior while enabling explicit local-only measurements."""
    import asyncpg

    connection = await asyncpg.connect(dsn=dsn, timeout=timeout, command_timeout=command_timeout)
    if os.environ.get('FACTORY_STATE_METRICS_FILE'):
        connection.add_query_logger(record_query)
    return connection


def summarize(path: Path) -> dict[str, int | float]:
    """Aggregate small counters from all trusted processes in one Actions job."""
    totals: dict[str, int | float] = {'read': 0, 'write': 0, 'ddl': 0, 'control': 0, 'failed': 0, 'milliseconds': 0.0}
    if path.exists():
        for line in path.read_text().splitlines():
            item = json.loads(line)
            kind = item['kind']
            totals[kind] += 1
            totals['failed'] += int(item['failed'])
            totals['milliseconds'] += item['milliseconds']
    totals['milliseconds'] = round(totals['milliseconds'], 3)
    return totals


def main() -> None:
    """Print counts only; no diagnostic artifact or raw query log is uploaded."""
    parser = argparse.ArgumentParser()
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.path), sort_keys=True))


if __name__ == '__main__':
    main()
