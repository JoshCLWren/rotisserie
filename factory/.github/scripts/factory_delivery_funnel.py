"""Event-window delivery funnels and normalized advance-commit credit."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

ADVANCE = re.compile(r'(?m)^\s*(?:\*\s*)?factory: advance (?:PR )?#\d+ with (.+)$')
BRANCH = re.compile(r'^factory/(\d+)-\d+-')
WORKER = re.compile(r'(?m)^Worker:\s*opencode-(?:free-model|nvidia|omniroute)-factory-(\d+)\s*$')
MODEL = re.compile(r'(?m)^Model:\s*(\S+)')
Fetch = Callable[[str], object]


def records(value: object) -> list[dict[str, object]]:
    """Require complete structured API rows instead of silently showing zero counts."""
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError('invalid funnel API list')
    return [dict(row) for row in value]


def timestamp(value: object) -> datetime | None:
    """Parse an explicit timestamp; absent event times mean no event."""
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('funnel event timestamp must include timezone')
    return parsed.astimezone(timezone.utc)


def commit_credit(
    messages: Sequence[str], pins: Sequence[Mapping[str, str]],
) -> Counter[tuple[str, str]]:
    """Count advance lines, including nested squash subjects and served-model suffixes.

    Display names shared by several workers are ambiguous for worker credit:
    they map to ``unattributed`` while retaining the single agreed model, or
    the display name itself when colliding rows disagree on the model.
    """
    workers_by_name: dict[str, set[str]] = {}
    models_by_name: dict[str, set[str]] = {}
    for row in pins:
        name = row['display_name']
        workers_by_name.setdefault(name, set()).add(row['worker'])
        models_by_name.setdefault(name, set()).add(row['model'])
    aliases: dict[str, tuple[str, str]] = {}
    for name, workers in workers_by_name.items():
        worker = next(iter(workers)) if len(workers) == 1 else 'unattributed'
        models = models_by_name[name]
        model = next(iter(models)) if len(models) == 1 else name
        aliases[name] = (worker, model)
    counts: Counter[tuple[str, str]] = Counter()
    for message in messages:
        for match in ADVANCE.finditer(message):
            label = re.sub(r'\s+\(#\d+\)$', '', match[1].strip())
            served = re.fullmatch(r'(.+) \(served: ([A-Za-z0-9._:/@+,\-]+)\)', label)
            base = served[1] if served else label
            worker, model = aliases.get(base, ('unattributed', base))
            models = sorted(set(served[2].split(','))) if served else [model]
            for upstream in models:
                counts[(worker, upstream)] += 1 / len(models)
    return counts


def paginated(fetch: Fetch, endpoint: str) -> list[dict[str, object]]:
    """Read every API page; errors propagate rather than imply a complete sample."""
    rows: list[dict[str, object]] = []
    for page in range(1, 101):
        batch = records(fetch(f'{endpoint}?per_page=100&page={page}'))
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise ValueError('funnel pagination budget exhausted')


def collect_delivery_funnel(
    fetch: Fetch, pins: Sequence[Mapping[str, str]], *, now: datetime, cache_path: Path,
) -> list[dict[str, object]]:
    """Collect 14/30-day events; cache normalized merged attribution by immutable SHA.

    Each merged PR contributes one unit of credit split among its advance commits.
    Original commit and squash-body multisets are unioned by maximum multiplicity,
    so the same advances preserved in both places are not credited twice.
    """
    cache: dict[str, object] = {}
    if cache_path.exists():
        loaded = json.loads(cache_path.read_text())
        if isinstance(loaded, dict) and loaded.get('version') == 1:
            merges = loaded.get('merges', {})
            if not isinstance(merges, dict):
                raise ValueError('invalid funnel cache')
            cache = dict(merges)
    retained: dict[str, object] = {}
    cutoff = now - timedelta(days=30)
    prs: list[dict[str, object]] = []
    for page in range(1, 101):
        batch = records(fetch(f'pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}'))
        prs.extend(pr for pr in batch if (timestamp(pr.get('updated_at')) or now) >= cutoff)
        if len(batch) < 100 or any((timestamp(pr.get('updated_at')) or now) < cutoff for pr in batch):
            break
    else:
        raise ValueError('recent PR pagination budget exhausted')
    totals: dict[tuple[str, str, int], dict[str, object]] = {}

    def add(group: str, name: str, days: int, field: str, value: float = 1) -> None:
        key = (group, name, days)
        row = totals.setdefault(key, {
            'group': group, 'name': name, 'days': days,
            'opened': 0.0, 'merged': 0.0, 'closed': 0.0, 'commit_share': 0.0,
        })
        previous = row[field]
        if not isinstance(previous, (float, int)):
            raise ValueError('invalid funnel accumulator')
        row[field] = previous + value

    for pin in pins:
        for days in (14, 30):
            add('worker', pin['worker'], days, 'opened', 0)
            add('model', pin['model'], days, 'opened', 0)
    pins = list(pins)
    for pr in prs:
        body = str(pr.get('body') or '')
        historical = re.search(r'Produced by fixed-model Factory (\d+) \((.+)\)\.', body)
        model_match = MODEL.search(body)
        if historical and model_match:
            pins.append({'worker': historical[1], 'model': model_match[1],
                         'display_name': historical[2]})
    by_worker = {row['worker']: row['model'] for row in pins}
    unique_prs = {str(pr.get('number')): pr for pr in prs}
    for pr in unique_prs.values():
        head = pr.get('head')
        branch = str(head.get('ref') or '') if isinstance(head, dict) else ''
        body = str(pr.get('body') or '')
        worker_match = BRANCH.search(branch) or WORKER.search(body)
        labels = records(pr.get('labels') or [])
        if worker_match is None and not any(label.get('name') == 'factory' for label in labels):
            continue
        worker = worker_match[1] if worker_match else 'unattributed'
        model_match = MODEL.search(body)
        model = model_match[1] if model_match else by_worker.get(worker, 'unattributed')
        merged = timestamp(pr.get('merged_at'))
        events = {'opened': timestamp(pr.get('created_at')), 'merged': merged,
                  'closed': None if merged else timestamp(pr.get('closed_at'))}
        for days in (14, 30):
            for field, observed in events.items():
                if observed is not None and now - timedelta(days=days) <= observed <= now:
                    add('worker', worker, days, field)
                    add('model', model, days, field)
        if merged is None or not cutoff <= merged <= now:
            continue
        sha = str(pr.get('merge_commit_sha') or '')
        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError('merged PR has no immutable merge SHA')
        if sha not in cache:
            commits = paginated(fetch, f"pulls/{pr['number']}/commits")
            messages = []
            for commit in commits:
                detail = commit.get('commit')
                if not isinstance(detail, dict) or not isinstance(detail.get('message'), str):
                    raise ValueError('invalid original commit metadata')
                messages.append(detail['message'])
            merge = fetch(f'commits/{sha}')
            detail = merge.get('commit') if isinstance(merge, dict) else None
            if not isinstance(detail, dict) or not isinstance(detail.get('message'), str):
                raise ValueError('invalid squash/merge metadata')
            counts = commit_credit(messages, pins) | commit_credit([detail['message']], pins)
            cache[sha] = [{'worker': key[0], 'model': key[1], 'count': value}
                          for key, value in counts.items()]
        retained[sha] = cache[sha]
        credits = records(cache[sha])
        # A merged PR without advance commits still counts as a merge; it just
        # supplies no worker/model contribution credit for that immutable SHA.
        if not credits:
            continue
        parsed_counts: list[float] = []
        for row in credits:
            raw_count = row.get('count')
            try:
                count = float(str(raw_count))
            except (TypeError, ValueError):
                raise ValueError('invalid funnel cache count') from None
            if not math.isfinite(count) or count <= 0:
                raise ValueError('invalid funnel cache count')
            parsed_counts.append(count)
        total = sum(parsed_counts)
        if total <= 0 or not math.isfinite(total):
            raise ValueError('invalid funnel cache count')
        for credit, count in zip(credits, parsed_counts, strict=True):
            share = count / total
            for days in (14, 30):
                if merged >= now - timedelta(days=days):
                    add('worker', str(credit['worker']), days, 'commit_share', share)
                    add('model', str(credit['model']), days, 'commit_share', share)
    for group, name in {(group, name) for group, name, _ in totals}:
        for days in (14, 30):
            add(group, name, days, 'opened', 0)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({'version': 1, 'merges': retained}, separators=(',', ':')))
    return [totals[key] for key in sorted(totals)]
