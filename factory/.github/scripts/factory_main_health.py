#!/usr/bin/env python3
"""Exact-SHA product CI health for ``main`` and the serialized merge gate.

Every factory merge route (the ready-merge drain and the workers' canonical
merge route) goes through this module, so merges are serialized and each
resulting ``main`` commit is verified before the next merge:

* ``classify`` turns the product CI runs recorded on one exact ``main`` SHA into
  ``green``, ``pending``, ``red`` or ``unverified``. No run means *unverified*,
  never green: factory merges use ``GITHUB_TOKEN``, which fires no push event,
  so a merged ``main`` has no CI until something dispatches it.
* ``merge_decision`` admits a merge only onto a verified green ``main``. A red
  ``main`` admits only the main-repair lane (a PR labelled ``main-breakage``), so
  a broken ``main`` cannot deadlock the merge system.
* ``claim`` takes an atomic, exclusive merge claim on the exact ``main`` SHA (an
  annotated tag ref; GitHub refuses a second create), so two merge actors that
  both saw the same green ``main`` cannot both merge onto it.
* ``after-merge`` releases the claim and dispatches product CI on the new
  ``main`` (deduplicated), so the next merge waits for its verification.

Durable evidence is the CI runs on the exact ``main`` SHA themselves.

The product CI workflow files come from ``FACTORY_MAIN_CI_WORKFLOWS`` (comma
list of workflow file names, default ``ci.yml``). Each must accept
``workflow_dispatch``; a path-filtered workflow is still dispatchable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

FAILING_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "startup_failure",
    "timed_out",
}
# Events that run product CI on a ``main`` commit. Pull-request runs test a
# merge preview, not the commit on ``main``.
MAIN_EVENTS = frozenset({"push", "workflow_dispatch"})
DEFAULT_WORKFLOWS = ("ci.yml",)
REPAIR_LABEL = "main-breakage"
CLAIM_PREFIX = "factory-merge-claim/"
# A claim outlives one merge plus its bookkeeping; an older one belongs to a
# crashed actor and is reclaimable.
CLAIM_TTL_SECONDS = 900
TAGGER = {
    "name": "github-actions[bot]",
    "email": "41898282+github-actions[bot]@users.noreply.github.com",
}

State = Literal["green", "pending", "red", "unverified"]


class RunSummary(TypedDict):
    id: int
    workflow: str
    event: str
    status: str
    conclusion: str


class Health(TypedDict):
    sha: str
    state: State
    workflows: list[str]
    runs: list[RunSummary]
    failing: list[str]
    pending: list[str]
    missing: list[str]
    explanation: str


class Decision(TypedDict):
    allow: bool
    lane: Literal["normal", "main-repair", "none"]
    reason: str


def workflow_runs(payload: object) -> Iterable[Mapping[str, object]]:
    """Yield workflow runs from one REST page or a slurped page list.

    Raises:
        ValueError: The response does not have the expected GitHub shape.
    """
    pages = payload if isinstance(payload, list) else [payload]
    for page in pages:
        if not isinstance(page, Mapping):
            raise ValueError("workflow-runs response page must be an object")
        runs = page.get("workflow_runs")
        if not isinstance(runs, list):
            raise ValueError("workflow-runs response is missing workflow_runs")
        for run in runs:
            if not isinstance(run, Mapping):
                raise ValueError("workflow run must be an object")
            yield run


def configured_workflows(raw: str | None = None) -> tuple[str, ...]:
    """Product CI workflow files from ``FACTORY_MAIN_CI_WORKFLOWS``."""
    value = os.environ.get("FACTORY_MAIN_CI_WORKFLOWS", "") if raw is None else raw
    names = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    for name in names:
        if "/" in name or not name.endswith((".yml", ".yaml")):
            raise ValueError(f"invalid product CI workflow file name: {name!r}")
    return names or DEFAULT_WORKFLOWS


def _workflow_file(run: Mapping[str, object]) -> str:
    return str(run.get("path") or "").rsplit("/", 1)[-1]


def classify(payload: object, sha: str, workflows: Sequence[str] = DEFAULT_WORKFLOWS) -> Health:
    """Classify the product CI evidence on one exact ``main`` SHA.

    Only push and workflow_dispatch runs of the configured workflows at ``sha``
    count. Per workflow the newest run (highest id) is the verdict, so a
    re-dispatched run supersedes an earlier flaky one on the same commit. A
    configured workflow with no run leaves ``main`` unverified.

    Precedence: red, then pending, then unverified, then green.
    """
    latest: dict[str, Mapping[str, object]] = {}
    for run in workflow_runs(payload):
        if run.get("head_sha") != sha or run.get("event") not in MAIN_EVENTS:
            continue
        name = _workflow_file(run)
        if name not in workflows:
            continue
        run_id = run.get("id")
        if not isinstance(run_id, int):
            raise ValueError("workflow run is missing its numeric id")
        current = latest.get(name)
        if current is None or int(str(current.get("id"))) < run_id:
            latest[name] = run

    runs: list[RunSummary] = []
    failing: list[str] = []
    pending: list[str] = []
    for name in workflows:
        run = latest.get(name)
        if run is None:
            continue
        status = str(run.get("status") or "")
        conclusion = str(run.get("conclusion") or "").lower()
        run_id = int(str(run.get("id")))
        runs.append(
            RunSummary(
                id=run_id,
                workflow=name,
                event=str(run.get("event") or ""),
                status=status,
                conclusion=conclusion,
            )
        )
        label = f"{name} run {run_id}"
        if status != "completed":
            pending.append(f"{label} ({status or 'queued'})")
        elif conclusion in FAILING_CONCLUSIONS:
            failing.append(f"{label} ({conclusion})")
        elif conclusion not in ("success", "neutral", "skipped"):
            failing.append(f"{label} ({conclusion or 'no conclusion'})")
    missing = [name for name in workflows if name not in latest]

    short = sha[:12]
    state: State
    if failing:
        state = "red"
        explanation = f"main {short} failed product CI: {', '.join(failing)}."
    elif pending:
        state = "pending"
        explanation = f"main {short} product CI is still running: {', '.join(pending)}."
    elif missing:
        state = "unverified"
        explanation = (
            f"main {short} has no product CI run for {', '.join(missing)}; "
            "it is unverified until CI is dispatched and passes."
        )
    else:
        state = "green"
        explanation = f"main {short} passed product CI ({len(runs)} workflow(s))."
    return Health(
        sha=sha,
        state=state,
        workflows=list(workflows),
        runs=runs,
        failing=failing,
        pending=pending,
        missing=missing,
        explanation=explanation,
    )


def summarize(payload: object, sha: str) -> dict[str, object]:
    """Legacy counts for push-triggered ``CI`` runs (kept for older callers)."""
    relevant: dict[int, Mapping[str, object]] = {}
    for run in workflow_runs(payload):
        if run.get("name") != "CI" or run.get("event") != "push":
            continue
        if run.get("head_sha") != sha:
            continue
        run_id = run.get("id")
        if not isinstance(run_id, int):
            raise ValueError("workflow run is missing its numeric id")
        relevant[run_id] = run
    failures = [
        f"CI run {run_id} ({str(run.get('conclusion') or '').lower()})"
        for run_id, run in sorted(relevant.items())
        if str(run.get("conclusion") or "").lower() in FAILING_CONCLUSIONS
    ]
    return {"failing": len(failures), "failing_names": ", ".join(failures), "total": len(relevant)}


def merge_decision(health: Health, labels: Iterable[str]) -> Decision:
    """Whether a merge may land onto ``main`` in this health state."""
    repair = REPAIR_LABEL in set(labels)
    short = health["sha"][:12]
    if health["state"] == "green":
        return Decision(allow=True, lane="main-repair" if repair else "normal",
                        reason=f"main {short} is verified green")
    if health["state"] == "red":
        if repair:
            return Decision(allow=True, lane="main-repair",
                            reason=f"main {short} is red; {REPAIR_LABEL} PRs may repair it")
        return Decision(allow=False, lane="none",
                        reason=f"main {short} is red; only {REPAIR_LABEL} PRs may merge")
    if health["state"] == "pending":
        return Decision(allow=False, lane="none",
                        reason=f"main {short} product CI is still running; merges wait for it")
    return Decision(allow=False, lane="none",
                    reason=f"main {short} is unverified (no product CI run); merges wait for it")


def claim_is_stale(created: str, now: float, ttl: int = CLAIM_TTL_SECONDS) -> bool:
    """Whether a claim tag's ISO-8601 tagger date is older than ``ttl``."""
    try:
        stamp = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return True
    return now - stamp > ttl


# --- GitHub I/O (thin; everything above is pure) -----------------------------


class GitHubError(RuntimeError):
    """A gh call failed."""


def _gh(args: list[str], *, payload: object | None = None) -> str:
    proc = subprocess.run(
        ["gh", *args],
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        raise GitHubError(proc.stderr.strip() or f"gh {' '.join(args[:2])} failed")
    return proc.stdout


def _api(path: str, *, method: str = "GET", payload: object | None = None) -> object:
    args = ["api", "-X", method, path]
    if payload is not None:
        args += ["--input", "-"]
    out = _gh(args, payload=payload)
    return json.loads(out) if out.strip() else None


def main_sha(repo: str, branch: str = "main") -> str:
    data = _api(f"repos/{repo}/commits/{branch}")
    sha = str(data.get("sha") or "") if isinstance(data, Mapping) else ""
    if len(sha) != 40:
        raise GitHubError(f"could not resolve {branch} of {repo}")
    return sha


def fetch_runs(repo: str, sha: str, workflows: Sequence[str]) -> list[object]:
    """One call per configured workflow, filtered to the exact SHA."""
    pages: list[object] = []
    for name in workflows:
        pages.append(_api(f"repos/{repo}/actions/workflows/{name}/runs?head_sha={sha}&per_page=100"))
    return pages


def evaluate(
    repo: str,
    sha: str | None = None,
    workflows: Sequence[str] | None = None,
    *,
    runs: object | None = None,
) -> Health:
    """Classify main; ``runs`` lets a caller hand over an already fetched payload."""
    names = tuple(workflows) if workflows else configured_workflows()
    target = sha or main_sha(repo)
    payload = runs if runs is not None else fetch_runs(repo, target, names)
    return classify(payload, target, names)


def dispatch_verification(repo: str, health: Health, branch: str = "main") -> list[str]:
    """Dispatch each missing product CI workflow on ``branch``; returns what ran.

    Only an *unverified* SHA is dispatched (a pending or completed run already
    exists otherwise), and only the workflows with no run, so repeated drains
    do not stack duplicate CI.
    """
    if health["state"] != "unverified":
        return []
    dispatched: list[str] = []
    for name in health["missing"]:
        _gh(["workflow", "run", name, "--repo", repo, "--ref", branch])
        dispatched.append(name)
    return dispatched


def _claim_ref(sha: str) -> str:
    return f"tags/{CLAIM_PREFIX}{sha}"


def read_claim(repo: str, sha: str) -> dict[str, str] | None:
    try:
        ref = _api(f"repos/{repo}/git/ref/{_claim_ref(sha)}")
    except GitHubError as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            return None
        raise
    obj = ref.get("object") if isinstance(ref, Mapping) else None
    if not isinstance(obj, Mapping) or obj.get("type") != "tag":
        return {"created": "", "message": ""}
    tag = _api(f"repos/{repo}/git/tags/{obj.get('sha')}")
    if not isinstance(tag, Mapping):
        return {"created": "", "message": ""}
    tagger = tag.get("tagger") if isinstance(tag.get("tagger"), Mapping) else {}
    assert isinstance(tagger, Mapping)
    return {"created": str(tagger.get("date") or ""), "message": str(tag.get("message") or "")}


def release_claim(repo: str, sha: str) -> bool:
    try:
        _gh(["api", "-X", "DELETE", f"repos/{repo}/git/refs/{_claim_ref(sha)}"])
    except GitHubError as exc:
        if "404" in str(exc) or "Not Found" in str(exc) or "Reference does not exist" in str(exc):
            return False
        raise
    return True


def acquire_claim(repo: str, sha: str, pr: int, *, now: float | None = None) -> bool:
    """Atomically claim the right to merge onto ``sha``; False if another holds it."""
    stamp = now if now is not None else time.time()
    existing = read_claim(repo, sha)
    if existing is not None:
        if not claim_is_stale(existing["created"], stamp):
            return False
        release_claim(repo, sha)
    tag = _api(
        f"repos/{repo}/git/tags",
        method="POST",
        payload={
            "tag": f"{CLAIM_PREFIX}{sha}",
            "message": f"factory merge claim: pr={pr} run={os.environ.get('GITHUB_RUN_ID', '')}",
            "object": sha,
            "type": "commit",
            "tagger": {**TAGGER, "date": datetime.fromtimestamp(stamp, timezone.utc).isoformat()},
        },
    )
    tag_sha = str(tag.get("sha") or "") if isinstance(tag, Mapping) else ""
    if not tag_sha:
        raise GitHubError("claim tag object was not created")
    try:
        _api(f"repos/{repo}/git/refs", method="POST",
             payload={"ref": f"refs/{_claim_ref(sha)}", "sha": tag_sha})
    except GitHubError as exc:
        if "422" in str(exc) or "already exists" in str(exc):
            return False
        raise
    return True


def pr_labels(repo: str, pr: int) -> list[str]:
    data = _api(f"repos/{repo}/issues/{pr}")
    labels = data.get("labels") if isinstance(data, Mapping) else None
    return [str(item.get("name")) for item in labels or [] if isinstance(item, Mapping)]


def gate(
    repo: str,
    pr: int,
    *,
    labels: Sequence[str] | None = None,
    health: Health | None = None,
) -> dict[str, object]:
    """Evaluate ``main`` for one candidate PR and claim the merge if admitted.

    ``labels`` (from the caller's ready-PR listing) and ``health`` (the drain's
    snapshot from earlier in the same run) avoid re-fetching. A snapshot is
    reused only while main's SHA is unchanged; that one cheap read is what keeps
    the claim bound to the commit actually on ``main``.
    """
    current = main_sha(repo)
    if health is None or health["sha"] != current:
        health = evaluate(repo, current)
    decision = merge_decision(health, labels if labels is not None else pr_labels(repo, pr))
    dispatched = dispatch_verification(repo, health)
    result: dict[str, object] = {**decision, "main": health, "dispatched": dispatched, "claimed": False}
    if not decision["allow"]:
        return result
    if not acquire_claim(repo, health["sha"], pr):
        result.update(allow=False, lane="none",
                      reason=f"another merge already claimed main {health['sha'][:12]}")
        return result
    result["claimed"] = True
    return result


def after_merge(repo: str, previous: str) -> dict[str, object]:
    """Release the claim on ``previous`` and dispatch CI for the resulting main."""
    released = release_claim(repo, previous)
    health = evaluate(repo)
    dispatched = dispatch_verification(repo, health) if health["sha"] != previous else []
    return {"released": released, "main": health, "dispatched": dispatched}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", help="legacy: summarize stdin workflow runs for this SHA")
    sub = parser.add_subparsers(dest="command")
    for name in ("evaluate", "ensure-verification"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", required=True)
        cmd.add_argument("--sha", dest="target")
    cmd = sub.add_parser("gate")
    cmd.add_argument("--repo", required=True)
    cmd.add_argument("--pr", type=int, required=True)
    cmd.add_argument("--labels", help="comma list from the caller's PR listing (skips a fetch)")
    cmd.add_argument("--health-file", type=Path, help="this run's evaluate/ensure-verification JSON")
    cmd = sub.add_parser("after-merge")
    cmd.add_argument("--repo", required=True)
    cmd.add_argument("--previous", required=True)
    cmd = sub.add_parser("release")
    cmd.add_argument("--repo", required=True)
    cmd.add_argument("--sha", dest="target", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command is None:
            if not args.sha:
                print("--sha or a subcommand is required", file=sys.stderr)
                return 2
            print(json.dumps(summarize(json.load(sys.stdin), args.sha), sort_keys=True))
            return 0
        if args.command == "evaluate":
            out: object = evaluate(args.repo, args.target)
        elif args.command == "ensure-verification":
            health = evaluate(args.repo, args.target)
            out = {"main": health, "dispatched": dispatch_verification(args.repo, health)}
        elif args.command == "gate":
            labels = None if args.labels is None else [x for x in args.labels.split(",") if x]
            snapshot = None
            if args.health_file is not None:
                loaded = json.loads(args.health_file.read_text(encoding="utf-8"))
                snapshot = loaded.get("main", loaded) if isinstance(loaded, dict) else None
            out = gate(args.repo, args.pr, labels=labels, health=snapshot)
        elif args.command == "after-merge":
            out = after_merge(args.repo, args.previous)
        else:
            out = {"released": release_claim(args.repo, args.target)}
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"invalid workflow-runs response: {exc}", file=sys.stderr)
        return 2
    except GitHubError as exc:
        print(f"GitHub unavailable: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
