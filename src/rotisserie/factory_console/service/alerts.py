"""Factory health alerts: the signals that catch a stalled factory within one check.

On Nov 26, 2026 a review-stage import error crashed every review verdict for
5.5 hours, and an API budget burn crashed the dispatcher for an hour. Each
check below fires on one of them within minutes.

GitHub calls per pass: ``turning.gather`` (its own reads), the worker run list
for ``FAILURE_WINDOW`` plus 3 reads per failed run (at most ``FAILURE_LIMIT``
runs), and 3 more: open pulls, issue comments since ``VERDICT_WINDOW``, and the
most recently updated closed pulls.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from rotisserie.factory_console.github import GitHub, Obj, integer, items, string
from rotisserie.factory_console.service import failures, turning
from rotisserie.factory_console.tracker import Clock

FAILURE_WINDOW = timedelta(minutes=20)
FAILURE_REPEATS = 3
FAILURE_LIMIT = 25
VERDICT_WINDOW = timedelta(minutes=45)
MERGE_WINDOW = timedelta(minutes=90)
WAITING_LABELS = ("factory:review", "factory:ready")
VERDICT_MARKER = "semantic-review-v1:"
NO_ERROR = ("", "(no error annotation)")


@dataclass(frozen=True)
class Alert:
    key: str
    kind: str
    text: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def repeated_failures(rows: Iterable[failures.WorkerFailure]) -> list[Alert]:
    """One alert per worker error seen ``FAILURE_REPEATS`` or more times."""
    errors: Counter[str] = Counter()
    classes: dict[str, Counter[str]] = {}
    for row in rows:
        if row.error in NO_ERROR:
            continue
        errors[row.error] += 1
        classes.setdefault(row.error, Counter())[row.outcome_class or "unrecorded"] += 1
    found = []
    for error, count in errors.most_common():
        if count < FAILURE_REPEATS:
            break
        recorded = ", ".join(f"{n} {name}" for name, n in classes[error].most_common())
        minutes = int(FAILURE_WINDOW.total_seconds() // 60)
        text = f"{count} worker runs failed the same way in {minutes}m ({recorded}): {error[:200]}"
        found.append(Alert(f"failure:{error[:80]}", "repeated-failure", text))
    return found


def state_alert(verdict: dict[str, object]) -> Alert | None:
    name = str(verdict.get("verdict") or "")
    if name in ("", turning.TURNING):
        return None
    return Alert("state", "factory-state", f"factory is {name}: {verdict.get('text') or ''}")


def waiting_prs(pulls: Sequence[Obj]) -> list[int]:
    found = []
    for pull in pulls:
        names = {string(label, "name") for label in items(pull.get("labels"))}
        if names & set(WAITING_LABELS):
            found.append(integer(pull, "number") or 0)
    return sorted(found)


def verdict_alert(waiting: Sequence[int], comments: Sequence[Obj]) -> Alert | None:
    if not waiting or any(VERDICT_MARKER in string(row, "body") for row in comments):
        return None
    minutes = int(VERDICT_WINDOW.total_seconds() // 60)
    text = f"no review verdict in {minutes}m while {len(waiting)} PRs wait in review or ready"
    return Alert("verdicts", "no-verdict", text)


def merge_alert(waiting: Sequence[int], closed: Sequence[Obj], now: datetime) -> Alert | None:
    since = _stamp(now - MERGE_WINDOW)
    if not waiting or any(string(row, "merged_at") >= since for row in closed):
        return None
    minutes = int(MERGE_WINDOW.total_seconds() // 60)
    text = f"no PR merged in {minutes}m while {len(waiting)} PRs wait in review or ready"
    return Alert("merges", "no-merge", text)


def check(github: GitHub, *, clock: Clock) -> list[Alert]:
    """Every alert that holds now, most urgent first."""
    now = clock.now()
    found = repeated_failures(
        failures.worker_failures(github, now - FAILURE_WINDOW, limit=FAILURE_LIMIT)
    )
    state = turning.assess(turning.gather(github, now=now, clock=clock)).to_dict()
    alert = state_alert(state)
    if alert:
        found.append(alert)
    waiting = waiting_prs(items(github.repo_api("pulls?state=open&per_page=100")))
    comments = items(
        github.repo_api(
            f"issues/comments?since={_stamp(now - VERDICT_WINDOW)}"
            "&sort=created&direction=desc&per_page=100"
        )
    )
    closed = items(github.repo_api("pulls?state=closed&sort=updated&direction=desc&per_page=50"))
    for extra in (verdict_alert(waiting, comments), merge_alert(waiting, closed, now)):
        if extra:
            found.append(extra)
    return found


def changes(previous: dict[str, Alert], current: Sequence[Alert]) -> list[tuple[str, Alert]]:
    """What a follow pass reports: alerts that started or changed, then the ones that cleared."""
    now = {alert.key: alert for alert in current}
    started = [("alert", a) for k, a in now.items() if previous.get(k) != a]
    cleared = [("cleared", a) for k, a in previous.items() if k not in now]
    return started + cleared
