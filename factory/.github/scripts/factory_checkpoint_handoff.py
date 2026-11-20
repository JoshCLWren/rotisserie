#!/usr/bin/env python3
"""Persist ordinary issue PRs independently of model execution and validation.

This trusted-main controller reads GitHub objects as data. It never checks out or
executes checkpoint source, changes an assigned PR head, or grants review approval.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

PREFIX = "factory/work-checkpoints/"
SHA = re.compile(r"[a-f0-9]{40}")
META = re.compile(
    r"checkpoint\(factory\): worker ([1-9][0-9]*) run ([1-9][0-9]*) "
    r"(issue|pr) #([1-9][0-9]*) \((?:unreviewed recovery only|persisted work; validation pending)\)"
)
WORKFLOW = ".github/workflows/free-model-factory-entry.yml"


def api(endpoint: str, method: str = "GET", **fields: object) -> object:
    """Make a bounded API request, preserving boolean JSON request values."""
    cmd = ["gh", "api", "--method", method, endpoint]
    payload = None
    if fields:
        cmd.extend(["--input", "-"])
        payload = json.dumps(fields)
    result = subprocess.run(cmd, input=payload, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError(f"GitHub {method} {endpoint}: {result.stderr[:300]}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def obj(endpoint: str) -> dict:
    """Require an object response rather than accepting truncated/malformed data."""
    value = api(endpoint)
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected GitHub object: {endpoint}")
    return value


def pages(endpoint: str) -> list[dict]:
    """Read complete lists, failing closed at a bounded pagination limit."""
    result = []
    for page in range(1, 101):
        join = "&" if "?" in endpoint else "?"
        data = api(f"{endpoint}{join}per_page=100&page={page}")
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise RuntimeError(f"Expected GitHub list: {endpoint}")
        result.extend(data)
        if len(data) < 100:
            return result
    raise RuntimeError("Checkpoint pagination exceeded bound; no destructive action")


def verified_run(repo: str, run_id: str) -> dict:
    """Accept only this repository's canonical fixed-model worker runs."""
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Invalid factory run ID")
    run = obj(f"repos/{repo}/actions/runs/{run_id}")
    if (
        run.get("path", "").split("@", 1)[0] != WORKFLOW
        or run.get("repository", {}).get("full_name") != repo
        or run.get("head_repository", {}).get("full_name") != repo
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
        or str(run.get("id")) != run_id
    ):
        raise ValueError("Checkpoint does not belong to a trusted same-repository worker run")
    return run


def checkpoint(repo: str, run_id: str) -> dict | None:
    """Read the exact named remote ref and its complete checkpoint ancestry."""
    refs = pages(f"repos/{repo}/git/matching-refs/heads/{PREFIX}")
    refs = [ref for ref in refs if ref.get("ref") == f"refs/heads/{PREFIX}{run_id}"
            or ref.get("ref", "").endswith(f"/{run_id}")]
    if len(refs) > 1:
        raise ValueError("Ambiguous checkpoint refs for one run")
    if not refs:
        return None
    head = refs[0]["object"]["sha"]
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError("Invalid checkpoint SHA")
    tip = obj(f"repos/{repo}/git/commits/{head}")
    cursor = tip
    # Once a real worker resumes this branch its tip is an ordinary commit.
    # Discover immutable origin metadata without resetting/rejecting that work.
    for _ in range(200):
        message = cursor.get("message", "")
        match = META.fullmatch(message.splitlines()[0] if message else "")
        if match and match[2] == run_id:
            break
        parents = cursor.get("parents", [])
        if not parents:
            raise ValueError("No factory origin metadata on persisted branch")
        cursor = obj(f"repos/{repo}/git/commits/{parents[0]['sha']}")
    else:
        raise RuntimeError("Origin metadata search exceeded bound; retaining work")
    metadata = dict(re.findall(r"(?m)^([A-Za-z-]+): (.+)$", cursor.get("message", "")))
    common = {"head": head, "tree": tip["tree"]["sha"],
              "branch": refs[0]["ref"].removeprefix("refs/heads/"),
              "mode": match[3], "number": int(match[4]), "worker": match[1],
              "issue": metadata.get("Issue", ""), "model": metadata.get("Model", ""),
              "origin_head": cursor.get("sha", head), "resumed": cursor.get("sha", head) != head}
    base = metadata.get("Base-commit", "")
    if SHA.fullmatch(base):
        comparison = obj(f"repos/{repo}/compare/{base}...{head}")
        if comparison.get("status") not in {"ahead", "identical"}:
            raise ValueError("Persisted base is not an ancestor")
        return {**common, "base": base}
    for _ in range(200):
        parents = cursor.get("parents", [])
        if len(parents) != 1:
            raise ValueError("Legacy checkpoint ancestry must be linear")
        parent = parents[0]["sha"]
        ancestor = obj(f"repos/{repo}/git/commits/{parent}")
        text = ancestor.get("message", "")
        previous = META.fullmatch(text.splitlines()[0] if text else "")
        if not previous or previous.groups() != match.groups():
            return {**common, "base": parent}
        cursor = ancestor
    raise RuntimeError("Origin ancestry exceeded bound; retaining branch")


def tree(repo: str, sha: str) -> dict[str, tuple[str, str, str]]:
    """Read all paths and modes; refuse GitHub's truncated recursive trees."""
    data = obj(f"repos/{repo}/git/trees/{sha}?recursive=1")
    if data.get("truncated") is not False:
        raise RuntimeError("Incomplete tree; retaining checkpoint")
    return {item["path"]: (item["sha"], item["mode"], item["type"])
            for item in data["tree"] if item["type"] != "tree"}


def incorporated(repo: str, saved: dict, destination: str) -> bool:
    """Prove every changed/deleted path's bytes and mode exist at destination."""
    base_tree = tree(repo, saved["base"])
    saved_tree = tree(repo, saved["tree"])
    target_tree = tree(repo, destination)
    changed = {path for path in base_tree.keys() | saved_tree.keys()
               if base_tree.get(path) != saved_tree.get(path)}
    return bool(changed) and all(target_tree.get(path) == saved_tree.get(path) for path in changed)


def recovery_prs(repo: str, branch_name: str) -> list[dict]:
    """Find draft handoff PRs by the exact same-repository head branch."""
    owner = repo.split("/", 1)[0]
    branch = quote(f"{owner}:{branch_name}", safe="")
    return pages(f"repos/{repo}/pulls?state=all&head={branch}")


def marker(run_id: str) -> str:
    """Return the idempotency marker for links and metadata."""
    return f"<!-- factory-checkpoint-handoff:v1 run={run_id} -->"


OWNER_RE = re.compile(r"factory:(?:local|[1-9][0-9]*)")
STAGES = {"factory:building", "factory:review", "factory:changes-requested", "factory:ci",
          "factory:ready", "factory:blocked"}
HOLDS = {"factory:blocked", "needs-human", "do-not-merge", "security-review"}


def labels_of(value: dict) -> set[str]:
    """Read repository labels without trusting prose or commit metadata."""
    return {label["name"] for label in value.get("labels", [])}


def source_worker(run: dict) -> str:
    """Bind source identity to the trusted workflow's dispatch-derived run name."""
    found = re.fullmatch(r"Factory ([1-9][0-9]*) · fixed-model entry",
                        run.get("display_title", ""))
    if not found:
        raise ValueError("Canonical source run does not expose its dispatched factory")
    return found[1]


def check_write(repo: str, run_id: str, mode: str, number: int, worker: str,
                branch: str | None = None) -> bool:
    """Fence snapshot pushes on the actual active source assignment and owner."""
    run = verified_run(repo, run_id)
    if run.get("status") == "completed" or source_worker(run) != worker:
        return False
    source = obj(f"repos/{repo}/issues/{number}")
    labels = labels_of(source)
    if source.get("state") != "open" or labels & HOLDS:
        return False
    if {label for label in labels if OWNER_RE.fullmatch(label)} != {f"factory:{worker}"}:
        return False
    if branch:
        for candidate in recovery_prs(repo, branch):
            if candidate.get("state") != "open":
                continue
            current = labels_of(obj(f"repos/{repo}/issues/{candidate['number']}"))
            if current & HOLDS or {label for label in current if OWNER_RE.fullmatch(label)} - {
                    f"factory:{worker}"}:
                return False
    if mode == "pr":
        pr = obj(f"repos/{repo}/pulls/{number}")
        return (pr.get("head", {}).get("repo", {}).get("full_name") == repo
                and pr.get("base", {}).get("ref") == "main" and pr.get("state") == "open")
    return "pull_request" not in source


def issue_for(repo: str, saved: dict, original: dict | None) -> dict:
    """Resolve the actual original issue rather than inventing a recovery task."""
    number = saved.get("issue", "")
    if not re.fullmatch(r"[1-9][0-9]*", str(number)):
        if saved["mode"] == "issue":
            number = saved["number"]
        elif original:
            found = re.search(r"(?im)\b(?:closes|fixes|resolves)\s+#([1-9][0-9]*)",
                              original.get("body", ""))
            if not found:
                found = re.match(r"factory/[0-9]+-([0-9]+)-", original["head"]["ref"])
            number = found[1] if found else ""
    if not re.fullmatch(r"[1-9][0-9]*", str(number)):
        raise ValueError("Original issue linkage is required for normal persisted work")
    issue = obj(f"repos/{repo}/issues/{number}")
    if "pull_request" in issue:
        raise ValueError("Original issue linkage resolves to a pull request")
    return issue


def work_body(issue: dict, saved: dict, run: dict, run_id: str) -> str:
    """Make the actual issue objective and incomplete work executable by any agent."""
    # Raw issue context can contain "Do not close #parent" or other directives.
    # Only the deliberate scoped linkage below may close an issue on merge.
    context = re.sub(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b", "reference",
                     issue.get("body") or issue["title"])
    return (f"Closes #{issue['number']}.\n\n"
            f"{marker(run_id)}\n\n"
            f"Model: {saved['model']}\n"
            f"Worker: opencode-free-model-factory-{saved['worker']}\n"
            f"Source run: {run['html_url']}\n\n"
            "Owner objective and acceptance criteria (original issue):\n\n"
            f"{context}\n\n"
            "## Continuation\n\n"
            "This is the actual issue implementation PR. Continue fixing this branch. "
            "Work is persisted before validation; a model interruption or failed local check "
            "leaves this same PR available for another factory or interactive agent.\n\n"
            "Validation: incomplete until exact-head local checks, required CI and independent "
            "review pass. Publication is not approval or evidence of issue completion.\n")


def ready(repo: str, pr: dict) -> None:
    """Convert legacy generated drafts into ordinary resumable work PRs."""
    if pr.get("draft"):
        api("graphql", "POST", query=(
            "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id})"
            "{pullRequest{id isDraft}}}"), variables={"id": pr["node_id"]})


def validation_marker(run_id: str, head: str) -> str:
    """Identify actual local validation of an immutable persisted head."""
    return f"<!-- factory-persisted-validation:v1 run={run_id} head={head} -->"


def validated_head(repo: str, pr: dict, run_id: str) -> bool:
    """Only a trusted exact-head wrapper record can preserve validation state."""
    wanted = validation_marker(run_id, pr["head"]["sha"])
    comments = pages(f"repos/{repo}/issues/{pr['number']}/comments")
    return any(comment.get("user", {}).get("login") == "github-actions[bot]"
               and comment.get("body", "").strip() == wanted for comment in comments)


def newer_worker_run(repo: str, run: dict, worker: str) -> bool:
    """Fence old-run reconciliation against later sessions of the same factory.

    GitHub lists newest runs first. An incomplete bounded view is uncertainty,
    never permission to release an owner from an older execution.
    """
    data = obj(f"repos/{repo}/actions/workflows/free-model-factory-entry.yml/runs?per_page=100")
    runs = data.get("workflow_runs")
    if not isinstance(runs, list) or not runs:
        return True
    source_id = int(run["id"])
    title = f"Factory {worker} · fixed-model entry"
    if any(item.get("display_title") == title and int(item["id"]) > source_id for item in runs):
        return True
    return min(int(item["id"]) for item in runs) > source_id


def set_work_labels(repo: str, pr: dict, issue: dict, saved: dict, run: dict,
                    *, validated: bool, source_ended: bool = False) -> str:
    """Maintain one truthful owner/stage without stealing a newer live lease."""
    owner = f"factory:{saved['worker']}"
    source = issue if saved["mode"] == "issue" else obj(
        f"repos/{repo}/issues/{saved['number']}")
    source_labels = labels_of(source)
    source_owners = {label for label in source_labels if OWNER_RE.fullmatch(label)}
    pr_labels = labels_of(obj(f"repos/{repo}/issues/{pr['number']}"))
    if (source_owners - {owner} or source_labels & HOLDS or pr_labels & HOLDS
            or {label for label in pr_labels if OWNER_RE.fullmatch(label)} - {owner}):
        return "another live owner or hold preserved"
    if (run.get("status") == "completed" and (source_owners or any(
            OWNER_RE.fullmatch(label) for label in pr_labels))
            and newer_worker_run(repo, run, saved["worker"])):
        return "later factory execution or incomplete run view preserved"
    # A newer ordinary commit or lease belongs to its resumed worker, even if
    # that worker happens to have the same factory identity as this old run.
    if saved.get("resumed") or pr["head"]["sha"] != saved["head"]:
        return "resumed work and stage preserved"
    active = run.get("status") != "completed" and not source_ended
    if active and source_owners != {owner}:
        return "source assignment released; no ownership inferred from old metadata"
    expected_owner = owner if active else "factory:unowned"
    stage = "factory:review" if validated else (
        "factory:building" if active else "factory:changes-requested")
    for target, is_issue in ((pr, False), (issue, True)):
        current = obj(f"repos/{repo}/issues/{target['number']}")
        current_labels = labels_of(current)
        owners = {label for label in current_labels if OWNER_RE.fullmatch(label)}
        if owners - {owner} or current_labels & HOLDS:
            continue
        # Once released, another controller may have advanced this stage.
        if (not active and not owners and "factory:building" not in current_labels
                and current_labels & STAGES):
            continue
        labels = {label for label in current_labels
                  if label not in STAGES and label != "factory:unowned"
                  and not OWNER_RE.fullmatch(label)
                  and not (is_issue and label.startswith("ralph-status:"))}
        labels |= {"factory", stage, expected_owner}
        if is_issue:
            labels.add("ralph-status:in-review" if not active else "ralph-status:in-progress")
        api(f"repos/{repo}/issues/{target['number']}/labels", "PUT", labels=sorted(labels))
    return f"{stage}/{expected_owner}"


def record_producer(repo: str, pr: dict, saved: dict) -> None:
    """Use the canonical trusted controller for actual exact-head authorship."""
    head = pr["head"]["sha"]
    if head != saved["head"] or saved.get("resumed"):
        return  # An ordinary later commit belongs to its actual later producer.
    comments = pages(f"repos/{repo}/issues/{pr['number']}/comments")
    needle = (f"<!-- comic-pile-factory-head-contributor-v1:pr-{pr['number']}:"
              f"head-{head}:worker-{saved['worker']}:epoch-")
    if any(comment.get("user", {}).get("login") == "github-actions[bot]"
           and comment.get("body", "").startswith(needle) for comment in comments):
        return
    controller = os.environ.get("TRUSTED_REVIEW_CONTROLLER")
    if not controller:
        controller = str(Path(__file__).with_name("factory-review-controller.py"))
    result = subprocess.run(
        ["python3", controller, "record-contribution", "--worker", saved["worker"],
         "--pr", str(pr["number"]), "--head", head], capture_output=True, text=True, timeout=90,
        env={**os.environ, "GITHUB_REPOSITORY": repo},
    )
    if result.returncode:
        raise RuntimeError("Canonical exact-head producer recording failed; retry required")


def handoff_comment(repo: str, pr: dict, saved: dict, run: dict, run_id: str) -> None:
    """Leave actionable saved-head and validation evidence on the same issue PR."""
    tag = f"<!-- factory-persisted-handoff:v1 run={run_id} head={saved['head']} -->"
    comments = pages(f"repos/{repo}/issues/{pr['number']}/comments")
    if any(tag in comment.get("body", "") for comment in comments):
        return
    api(f"repos/{repo}/issues/{pr['number']}/comments", "POST", body=(
        f"{tag}\nPersisted work from Factory {saved['worker']} "
        f"(`{saved['model']}`), [source run]({run['html_url']}).\n"
        f"Saved head: `{saved['head']}` on `{saved['branch']}`.\n"
        "If this legacy tracking head is not yet on the work branch, fetch that ref "
        "and reconcile its changes here without replacing another owner’s work.\n"
        "Continue this issue PR and its existing acceptance criteria. Local validation "
        "is incomplete unless an exact-head wrapper validation record says otherwise. "
        "If the source run exited, fixes are available for the next eligible agent."
    ))


def publish(repo: str, run_id: str, *, cleanup: bool = False, source_ended: bool = False) -> dict:
    """Persist ordinary issue work, preserving live ownership and real merge gates."""
    run = verified_run(repo, run_id)
    saved = checkpoint(repo, run_id)
    if saved is None:
        return {"saved": False, "published": False, "validated": False}
    if source_worker(run) != saved["worker"]:
        raise ValueError("Snapshot producer does not match trusted source dispatch")
    original = obj(f"repos/{repo}/pulls/{saved['number']}") if saved["mode"] == "pr" else None
    issue = issue_for(repo, saved, original)
    branches = recovery_prs(repo, saved["branch"])
    if original:
        if (original.get("head", {}).get("repo", {}).get("full_name") != repo
                or original.get("base", {}).get("ref") != "main"):
            raise ValueError("Existing work must target same-repository main")
        pr = original
        # Legacy side PRs/refs retain their bytes until incorporation is proven.
        # No background reconciliation writes the original PR's branch.
    else:
        open_prs = [pr for pr in branches if pr.get("state") == "open"]
        if len(open_prs) > 1:
            raise RuntimeError("Multiple PRs for persisted branch; ownership requires reconciliation")
        if open_prs:
            pr = obj(f"repos/{repo}/pulls/{open_prs[0]['number']}")
        elif branches:
            pr = obj(f"repos/{repo}/pulls/{branches[0]['number']}")
            if cleanup and pr.get("merged"):
                return retire(repo, run_id, saved, [pr], pr["merge_commit_sha"])
            return {"saved": True, "published": False, "validated": False,
                    "work_pr": pr["number"], "detail": "closed issue work retained"}
        else:
            if run.get("status") != "completed" and not check_write(
                    repo, run_id, saved["mode"], saved["number"], saved["worker"]):
                raise RuntimeError("Source assignment no longer owns this persisted work")
            pr = api(f"repos/{repo}/pulls", "POST", title=issue["title"],
                     head=saved["branch"], base="main", body=work_body(issue, saved, run, run_id),
                     draft=False)
            if not isinstance(pr, dict):
                raise RuntimeError("GitHub did not return the normal issue PR")
    if pr.get("state") != "open":
        if cleanup and original and incorporated(repo, saved, pr.get("merge_commit_sha") or
                                                pr["head"]["sha"]):
            return retire(repo, run_id, saved, branches, pr["head"]["sha"])
        return {"saved": True, "published": False, "validated": False,
                "work_pr": pr["number"], "detail": "closed work retained"}
    generated = pr.get("title", "").startswith("Recovery:") or pr.get("draft")
    if generated:
        api(f"repos/{repo}/pulls/{pr['number']}", "PATCH", title=issue["title"],
            body=work_body(issue, saved, run, run_id), base="main")
        ready(repo, pr)
    complete = validated_head(repo, pr, run_id)
    stage = set_work_labels(repo, pr, issue, saved, run, validated=complete,
                            source_ended=source_ended)
    # Original repair bytes are already pushed atomically by the live writer.
    # Legacy saved refs not yet incorporated remain linked and never overwrite
    # a newer worker's assigned branch during background reconciliation.
    record_producer(repo, pr, saved)
    handoff_comment(repo, pr, saved, run, run_id)
    result = {"saved": True, "published": True, "validated": complete,
              "checkpoint_sha": saved["head"], "work_pr": pr["number"],
              "recovery_pr": pr["number"], "work_branch": pr["head"]["ref"],
              "checkpoint_branch": saved["branch"], "stage": stage}
    if cleanup and original and saved["branch"] != pr["head"]["ref"]:
        if incorporated(repo, saved, pr["head"]["sha"]):
            retired = retire(repo, run_id, saved, branches, pr["head"]["sha"])
            result["tracking_retired"] = retired.get("retired", False)
    return result


def retire(repo: str, run_id: str, saved: dict, prs: list[dict], destination: str) -> dict:
    """Retire only tracking refs or a merged/discarded canonical work branch."""
    run = verified_run(repo, run_id)
    current = checkpoint(repo, run_id)
    if run.get("status") != "completed" or not current or current["head"] != saved["head"]:
        return {"saved": True, "published": bool(prs), "validated": False,
                "detail": "live or moved persisted work retained"}
    for pr in prs:
        live = obj(f"repos/{repo}/pulls/{pr['number']}")
        labels = labels_of(live)
        if (labels & HOLDS or any(OWNER_RE.fullmatch(label) for label in labels)
                or (live.get("head", {}).get("ref") == saved["branch"]
                    and live.get("state") == "open")):
            return {"saved": True, "published": True, "validated": False,
                    "detail": "canonical work or live ownership retained"}
    if not SHA.fullmatch(destination) or not incorporated(repo, saved, destination):
        return {"saved": True, "published": bool(prs), "validated": False,
                "detail": "saved bytes not proven incorporated; retained"}
    api(f"repos/{repo}/git/refs/heads/{quote(saved['branch'], safe='/')}", "DELETE")
    return {"saved": True, "published": bool(prs), "validated": False,
            "retired": True, "incorporated_at": destination}


def record_delivery(repo: str, run_id: str, saved: dict, delivered_pr: int) -> dict:
    """Record actual local checks separately from persistence, keeping the same PR."""
    pr = obj(f"repos/{repo}/pulls/{delivered_pr}")
    head = pr.get("head", {}).get("sha", "")
    if pr.get("head", {}).get("repo", {}).get("full_name") != repo or not SHA.fullmatch(head):
        raise ValueError("Validated handoff must target same-repository work")
    comparison = obj(f"repos/{repo}/compare/{saved['head']}...{head}")
    if comparison.get("status") not in {"ahead", "identical"}:
        raise ValueError("Validated handoff must include persisted ancestry")
    if not incorporated(repo, saved, pr["head"]["sha"]):
        raise ValueError("Validated handoff is missing persisted work")
    tag = validation_marker(run_id, pr["head"]["sha"])
    if not validated_head(repo, pr, run_id):
        api(f"repos/{repo}/issues/{pr['number']}/comments", "POST", body=tag)
    return {"saved": True, "published": True, "validated": True,
            "work_pr": pr["number"], "recovery_pr": pr["number"],
            "work_branch": pr["head"]["ref"], "checkpoint_sha": saved["head"],
            "checkpoint_branch": saved["branch"]}


def enrich_outcome(path: Path, result: dict) -> None:
    """Add verified persistence without rewriting the worker's failing outcome."""
    outcome = json.loads(path.read_text())
    outcome["checkpoint_saved"] = result.get("saved", False)
    outcome["checkpoint_published"] = result.get("published", False)
    outcome["checkpoint_validated"] = result.get("validated", False)
    outcome["work_pr"] = result.get("work_pr", "")
    outcome["recovery_pr"] = result.get("work_pr", "")
    outcome["checkpoint_sha"] = result.get("checkpoint_sha", "")
    path.write_text(json.dumps(outcome) + "\n")


def main() -> int:
    """Publish one source run, reconcile completed work, or sweep known refs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--outcome", type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--delivered-pr", type=int)
    parser.add_argument("--check-write", action="store_true")
    parser.add_argument("--source-ended", action="store_true")
    parser.add_argument("--mode", choices=("issue", "pr"))
    parser.add_argument("--number", type=int)
    parser.add_argument("--worker")
    parser.add_argument("--work-branch")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("Invalid repository")
    if args.check_write:
        if not (args.run_id and args.mode and args.number and args.worker):
            parser.error("--check-write requires run, mode, number and worker")
        return int(not check_write(args.repo, args.run_id, args.mode, args.number, args.worker,
                                   args.work_branch))
    ids = [args.run_id] if args.run_id else []
    if args.sweep:
        refs = pages(f"repos/{args.repo}/git/matching-refs/heads/{PREFIX}")
        ids = sorted({ref["ref"].rsplit("/", 1)[-1] for ref in refs})
    if not ids and not args.sweep:
        parser.error("--run-id or --sweep is required")
    results = []
    failed = False
    for run_id in ids:
        try:
            result = publish(args.repo, run_id, cleanup=args.cleanup,
                             source_ended=args.source_ended)
            if args.delivered_pr and result.get("saved"):
                saved = checkpoint(args.repo, run_id)
                if saved:
                    result = record_delivery(args.repo, run_id, saved, args.delivered_pr)
            result["run_id"] = run_id
            results.append(result)
            if args.state_file:
                args.state_file.write_text(json.dumps(result) + "\n")
            if args.outcome:
                enrich_outcome(args.outcome, result)
        except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            failed = True
            result = {"run_id": run_id, "error": str(error)}
            if args.outcome:
                try:
                    saved = checkpoint(args.repo, run_id)
                    result["saved"] = bool(saved)
                    if saved:
                        result["checkpoint_sha"] = saved["head"]
                    enrich_outcome(args.outcome, result)
                except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired):
                    pass  # Independent reconciliation retries; never invent proof.
            results.append(result)
    print(json.dumps(results, indent=2))
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
