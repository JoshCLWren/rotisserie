#!/usr/bin/env python3
"""Long parks resolve themselves: one forced run, then progress or closure.

A PR the loop breaker parked ``PARK_SECONDS`` ago, still at a parked head, gets
one forced run on the model with the best track record for its stage
(``factory_model_track_record.best``), through the dispatcher's targeted path.
A ``park-retry`` store event keyed on the park time goes first, so a repeated
sweep never sends a second run for the same park.

Later sweeps judge the run from the store's worker outcomes:

* progress (a delivered or successful run): the park ends through
  ``factory_turso_loops.release`` and the pool takes the PR again;
* a counted run that changed nothing: the factory closes the PR with the
  findings and the last review, keeps its branch, and puts the linked issue
  back in the queue with a link to the closed PR;
* no counted run within ``LAPSE_SECONDS`` (the run never started or hit only
  capacity limits): another dispatch. A pause or quota outage never closes a PR.

A push ends the park by itself, because a park holds only its parked heads.
The exemptions of the park itself apply: owner override or claim, an outside
agent, an active incident, a live lease.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import factory_item_loop as loop
import factory_self_heal as heal
import factory_self_heal_rules as rules
import factory_turso_loops as turso_loops
from factory_work_policy_base import linked_issue_from_pr, stage_of

PARK_SECONDS = 12 * 3600
LAPSE_SECONDS = 6 * 3600
MAX_PER_SWEEP = 3
LOOKBACK_SECONDS = 3 * 86400
DISPATCH_WORKFLOW = "fixed-model-factory-dispatch.yml"
NOTE_MARKER = "factory-park-closed-v1"


@dataclass(frozen=True)
class Attempt:
    pr: int
    park: int
    number: int
    at: datetime
    model: str


def attempts_statement(repo: str) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT created_at, detail ->> 'pr' AS pr, detail ->> 'kind' AS kind,"
        " detail ->> 'park' AS park, detail ->> 'attempt' AS attempt,"
        " detail ->> 'model' AS model, detail ->> 'text' AS text FROM factory_state_events"
        " WHERE repo = :repo AND event_type = 'factory_self_heal'"
        " AND detail ->> 'kind' IN (:retry, :closed, :parked)"
        " AND created_at > unixepoch() - :lookback",
        {
            "repo": repo,
            "retry": rules.PARK_RETRY,
            "closed": rules.PARK_CLOSED,
            "parked": rules.LOOP_PARKED,
            "lookback": turso_loops.PARK_LOOKBACK_SECONDS,
        },
    )


def evidence(repo: str) -> dict[str, Any]:
    """Parks, forced attempts, closures and park texts."""
    import factory_turso_core as core

    db = core.store()
    park_rows, rows = db.reads([turso_loops.parks_statement(repo), attempts_statement(repo)])
    attempts: dict[tuple[int, int], list[Attempt]] = {}
    closed: set[tuple[int, int]] = set()
    texts: dict[int, str] = {}
    for row in sorted(rows, key=lambda row: int(row["created_at"])):
        if not str(row["pr"] or "").isdecimal():
            continue
        pr = int(row["pr"])
        if row["kind"] == rules.LOOP_PARKED:
            texts[pr] = str(row["text"] or "")
            continue
        key = (pr, int(row["park"] or 0))
        if row["kind"] == rules.PARK_CLOSED:
            closed.add(key)
            continue
        at = datetime.fromtimestamp(int(row["created_at"]), UTC)
        attempts.setdefault(key, []).append(
            Attempt(pr, key[1], int(row["attempt"] or 1), at, str(row["model"] or ""))
        )
    return {
        "parks": turso_loops.parks(park_rows),
        "attempts": attempts,
        "closed": closed,
        "texts": texts,
    }


def runs_since(repo: str, start: datetime, now: datetime) -> dict[str, list[loop.Run]]:
    """Worker outcomes since ``start``, read only when an open attempt needs judging."""
    import factory_turso_core as core

    seconds = int((now - start).total_seconds()) + 3600
    statement = turso_loops.runs_statement(repo, min(max(seconds, 3600), LOOKBACK_SECONDS))
    return turso_loops.runs_by_item(core.store().reads([statement])[0])


def judge(attempt: Attempt, runs: Sequence[loop.Run], now: datetime) -> str:
    """progress, stuck, lapsed or pending, from the counted runs since the attempt."""
    after = [run for run in runs if run.at >= attempt.at and run.counted]
    if any(not run.wasted for run in after):
        return "progress"
    if after:
        return "stuck"
    if (now - attempt.at).total_seconds() >= LAPSE_SECONDS:
        return "lapsed"
    return "pending"


def runtime_model(source: str, model: str) -> str:
    """The dispatcher's provider/model identifier, or "" when this source has none."""
    from factory_manual_model import ManualModelUnavailable, runtime_prefix, validate_model_syntax

    try:
        prefix = runtime_prefix(source)
        name = model if model.startswith(prefix) else f"{prefix}{model}"
        validate_model_syntax(source, name)
    except ManualModelUnavailable:
        return ""
    return name


def choose(stage: str, labels: set[str]) -> tuple[dict[str, str], str, str]:
    """Dispatcher inputs for a forced run, the model named, and why it was chosen."""
    import factory_model_track_record as track

    kind = track.action_kind({"kind": "pr", "stage": stage}, labels)
    found = track.best(kind)
    inputs = {"mode": "smoke", "action": "advance"}
    if found is None:
        return inputs, "auto", f"no proven model for {kind or 'this stage'}; normal selection"
    source, model, why = found
    name = runtime_model(source, model)
    if not name:
        return inputs, "auto", f"{source}/{model} has no dispatcher identifier; normal selection"
    return inputs | {"provider": source, "model": name, "force_slot": "true"}, name, why


def _number(row: Mapping[str, Any]) -> int:
    value = row.get("number")
    return value if isinstance(value, int) else 0


def _head(row: Mapping[str, Any]) -> str:
    head = row.get("head")
    return str(head.get("sha") or "") if isinstance(head, Mapping) else ""


def _branch(row: Mapping[str, Any]) -> str:
    head = row.get("head")
    return str(head.get("ref") or "") if isinstance(head, Mapping) else ""


def dispatch(
    github: heal.GitHub, store: heal.Store, pull: Mapping[str, Any], park: int, number: int
) -> dict[str, Any] | None:
    pr, head, names = _number(pull), _head(pull), heal.labels(pull)
    inputs, model, why = choose(stage_of(names) or "factory:review", names)
    text = (
        f"#{pr} has been parked for 12 hours. The factory sends one forced run"
        f" ({model}: {why}). If it changes nothing, the factory closes this PR, keeps the"
        " branch and puts its issue back in the queue."
    )
    ref = f"retry-{park}-a{number}"
    entry = heal.event(
        kind=rules.PARK_RETRY,
        pr=pr,
        head=head,
        ref=ref,
        text=text,
        park=park,
        attempt=number,
        model=model,
    )
    if not store.record([entry]):
        return None
    github.api(
        f"actions/workflows/{DISPATCH_WORKFLOW}/dispatches",
        "--method",
        "POST",
        payload={"ref": "main", "inputs": inputs | {"target": f"pr:{pr}"}},
    )
    body = rules.comment_body(kind=rules.PARK_RETRY, pr=pr, head=head, ref=ref, text=text)
    github.api(f"issues/{pr}/comments", "--method", "POST", payload={"body": body})
    return {"kind": rules.PARK_RETRY, "pr": pr, "attempt": number, "model": model, "text": text}


def release(github: heal.GitHub, pull: Mapping[str, Any], park: int) -> dict[str, Any] | None:
    pr, head = _number(pull), _head(pull)
    if not turso_loops.release(github.repo, pr, head, f"auto-{park}"):
        return None
    text = f"The factory's forced run on #{pr} made progress, so its park ended."
    body = rules.comment_body(
        kind=rules.LOOP_RELEASED, pr=pr, head=head or "none", ref=f"run-auto-{park}", text=text
    )
    github.api(f"issues/{pr}/comments", "--method", "POST", payload={"body": body})
    return {"kind": rules.LOOP_RELEASED, "pr": pr, "text": text}


def last_review(comments: Sequence[Mapping[str, Any]]) -> str:
    """The newest trusted semantic review on the PR, trimmed, or a note that none exists."""
    import factory_review_policy as policy

    for body in reversed(policy.controller_comment_bodies(comments)):
        first = body.splitlines()[0] if body else ""
        found = policy.parse_review_marker(first)
        if found:
            excerpt = " ".join(body.split("\n", 1)[-1].split())[:1200]
            return f"{found['verdict']} at `{found['head'][:12]}`: {excerpt}"
    return "none recorded"


def closing_text(
    pull: Mapping[str, Any], tries: Sequence[Attempt], verdict: str, park: str, review: str
) -> str:
    pr = _number(pull)
    models = ", ".join(dict.fromkeys(attempt.model for attempt in tries)) or "auto"
    return (
        f"Closed #{pr}: it was parked for 12 hours ({park or 'a worker loop'}), and the factory's"
        f" forced run ({models}) changed nothing. Last review: {review}. The branch `{_branch(pull)}` stays"
        f" at `{_head(pull)[:12]}`; the linked issue is back in the queue with this PR as"
        " context. Reopen this PR to undo."
    )


def requeue(github: heal.GitHub, issue: int, pull: Mapping[str, Any], park: int) -> str:
    from factory_adopter import MARKER_PREFIX
    from factory_no_progress_retirement import requeued_labels

    row = github.api(f"issues/{issue}")
    if not isinstance(row, Mapping) or row.get("state") != "open":
        return "closed"
    pr = _number(pull)
    marker = f"<!-- {MARKER_PREFIX}-{NOTE_MARKER}:issue-{issue}:pr-{pr}:park-{park} -->"
    if not any(marker in str(c.get("body") or "") for c in github.comments(issue)):
        note = (
            f"{marker}\n\n**Factory:** PR #{pr} ({pull.get('html_url') or ''}) was closed after a"
            " long park and a forced run that did not move it. Read that PR and its last review"
            f" before you start, and reuse what applies: branch `{_branch(pull)}`, head"
            f" `{_head(pull)}`."
        )
        github.api(f"issues/{issue}/comments", "--method", "POST", payload={"body": note})
    labels = requeued_labels(heal.labels(row))
    if labels is None:
        return "owned"
    github.api(f"issues/{issue}/labels", "--method", "PUT", payload={"labels": sorted(labels)})
    return "requeued"


def close(
    github: heal.GitHub,
    store: heal.Store,
    pull: Mapping[str, Any],
    found: Mapping[str, Any],
    park: int,
    verdict: str,
) -> dict[str, Any]:
    """Comment, hand the issue back and close; the store event comes last.

    Each step before the close is safe to repeat, so a failed pass is finished by
    the next one. A closure already in the store means an owner reopened the PR.
    """
    pr, head = _number(pull), _head(pull)
    tries = found["attempts"].get((pr, park), [])
    comments = github.comments(pr)
    text = closing_text(pull, tries, verdict, found["texts"].get(pr, ""), last_review(comments))
    ref = f"closed-{park}"
    posted = any(
        heal_row["kind"] == rules.PARK_CLOSED and heal_row["ref"] == ref
        for heal_row in rules.heals(comments, pr=pr)
    )
    if not posted:
        body = rules.comment_body(kind=rules.PARK_CLOSED, pr=pr, head=head, ref=ref, text=text)
        github.api(f"issues/{pr}/comments", "--method", "POST", payload={"body": body})
    issue = linked_issue_from_pr(
        {"headRefName": _branch(pull), "body": pull.get("body"), "title": pull.get("title")}
    )
    handoff = requeue(github, issue, pull, park) if issue else "no linked issue"
    github.api(f"pulls/{pr}", "--method", "PATCH", payload={"state": "closed"})
    store.record(
        [heal.event(kind=rules.PARK_CLOSED, pr=pr, head=head, ref=ref, text=text, park=park)]
    )
    return {
        "kind": rules.PARK_CLOSED,
        "pr": pr,
        "issue": issue,
        "issue_status": handoff,
        "text": text,
    }


def exempt(store: heal.Store, pull: Mapping[str, Any], incidents: frozenset[int]) -> str:
    names = heal.labels(pull)
    if loop.bypassed(names):
        return "owner"
    if any(name.startswith("agent:") for name in names):
        return "outside agent"
    if _number(pull) in incidents:
        return "incident"
    return store.holder(_number(pull)) or ""


def resolve_long_parks(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Send each long park one forced run, then release or close it on its outcome."""
    if not store.enabled():
        return []
    now = now or datetime.now(UTC)
    found = evidence(github.repo)
    due = []
    for pull in pulls:
        park = found["parks"].get(f"pr:{_number(pull)}")
        if park and _head(pull) in turso_loops.parked_heads(park):
            if now - park["at"] >= timedelta(seconds=PARK_SECONDS):
                due.append((pull, int(park["at"].timestamp())))
    if not due:
        return []
    from factory_self_heal_loops import incident_pulls

    incidents = incident_pulls(github, pulls)
    starts = [
        tries[-1].at
        for pull, park in due
        if (tries := found["attempts"].get((_number(pull), park)))
        and (_number(pull), park) not in found["closed"]
    ]
    runs = runs_since(github.repo, min(starts), now) if starts else {}
    done: list[dict[str, Any]] = []
    taken = 0
    for pull, park in due:
        pr = _number(pull)
        why = exempt(store, pull, incidents)
        if why:
            done.append({"pr": pr, "status": why})
            continue
        tries = found["attempts"].get((pr, park), [])
        if (pr, park) in found["closed"]:
            done.append({"pr": pr, "status": "reopened after closure"})
            continue
        if not tries:
            verdict = "due"
        else:
            verdict = judge(tries[-1], runs.get(f"pr:{pr}", []), now)
            if verdict == "lapsed":
                verdict = "due"
        if verdict == "pending":
            continue
        if budget.dry_run or taken >= MAX_PER_SWEEP or not budget.take():
            done.append({"pr": pr, "status": f"would-{verdict}"})
            continue
        taken += 1
        if verdict == "due":
            row = dispatch(github, store, pull, park, (tries[-1].number + 1) if tries else 1)
        elif verdict == "progress":
            row = release(github, pull, park)
        else:
            row = close(github, store, pull, found, park, verdict)
        if row:
            done.append(row)
    return done
