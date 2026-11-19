"""Run-local GitHub discovery, never persistent or authoritative coordination."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def snapshot_path(repo: str, kind: str) -> Path | None:
    """Resolve a per-job inventory file, isolated by repository and target kind."""
    directory = os.environ.get('FACTORY_DISCOVERY_DIR')
    if not directory:
        return None
    if kind not in ('issue', 'pr'):
        raise ValueError('invalid discovery kind')
    digest = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return Path(directory) / f'{digest}-{kind}.json'


def read_snapshot(repo: str, kind: str) -> list[dict[str, Any]] | None:
    """Read only the current job's inventory, without locks, TTLs or network IO."""
    path = snapshot_path(repo, kind)
    if path is None or not path.exists():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError('invalid run-local discovery snapshot')
    return value


def discover(repo: str, kind: str, fetch: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Discover once per trusted job; every mutation still rechecks GitHub live."""
    previous = read_snapshot(repo, kind)
    if previous is not None:
        return previous
    rows = fetch()
    path = snapshot_path(repo, kind)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(rows))
        temporary.replace(path)
    return rows


def update_labels(repo: str, kind: str, number: int, labels: list[dict[str, str]]) -> None:
    """Keep this dispatch pass's candidate list consistent with its own writes."""
    rows = read_snapshot(repo, kind)
    if rows is None:
        return
    for row in rows:
        if row.get('number') == number:
            row['labels'] = labels
    path = snapshot_path(repo, kind)
    assert path is not None
    path.write_text(json.dumps(rows))


def main() -> int:
    """Expose run-local reads to the REST CLI adapter without database access."""
    parser = argparse.ArgumentParser()
    parser.add_argument('repo')
    parser.add_argument('kind', choices=['issue', 'pr'])
    args = parser.parse_args()
    rows = read_snapshot(args.repo, args.kind)
    if rows is None:
        return 2
    json.dump(rows, sys.stdout)
    return 0


def canonical_target(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a targeted REST read to the scheduling policy's CLI shape."""
    result: dict[str, Any] = {
        'number': raw['number'], 'title': raw.get('title') or '',
        'body': raw.get('body') or '', 'labels': raw.get('labels') or [],
        'state': str(raw.get('state') or 'open').upper(),
        'createdAt': raw.get('created_at') or '',
        'updatedAt': raw.get('updated_at') or '',
    }
    if kind == 'pr':
        head, base = raw.get('head') or {}, raw.get('base') or {}
        result.update({
            'headRefName': head.get('ref'), 'headRefOid': head.get('sha'),
            'author': {'login': (raw.get('user') or {}).get('login') or ''},
            'isDraft': raw.get('draft', False),
            'isCrossRepository': (head.get('repo') or {}).get('full_name') != (base.get('repo') or {}).get('full_name'),
            'mergeable': 'MERGEABLE' if raw.get('mergeable') is True else 'CONFLICTING' if raw.get('mergeable') is False else 'UNKNOWN',
            'mergeStateStatus': str(raw.get('mergeable_state') or 'UNKNOWN').upper(),
        })
    return result


def discovered_pull_requests(repo: str, fetch: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Reuse the dispatch inventory in auxiliary recovery, preserving REST fields."""
    rows = read_snapshot(repo, 'pr')
    if rows is None:
        return fetch()
    return [{**row, 'draft': row.get('isDraft', False),
             'created_at': row.get('createdAt', ''),
             'updated_at': row.get('updatedAt', ''),
             'head': {'ref': row.get('headRefName'), 'sha': row.get('headRefOid')}}
            for row in rows]


if __name__ == '__main__':
    raise SystemExit(main())
