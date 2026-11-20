#!/usr/bin/env python3
"""Route failed production attempts into one leased, reviewed repair incident."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from collections.abc import Mapping, Sequence
from datetime import datetime

DEPLOY_JOB_NAME = "Migrate Neon and deploy exact main SHA"
RED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})


class GitHubQuotaDeferredError(RuntimeError):
    """GitHub cannot provide trustworthy evidence until its API quota resets."""


def fetch_run_jobs(run_id: int, *, repository: str) -> list[Mapping[str, object]] | None:
    """Fetch jobs for a completed deployment run."""
    try:
        payload = gh_json(["run", "view", str(run_id), "--repo", repository, "--json", "jobs"])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        return None
    return mappings(payload["jobs"])


def latest_attempted_deploy_run(
    payload: object,
    *,
    jobs_loader: object = None,
) -> Mapping[str, object] | None:
    """Return the newest completed deployment run that reached the deploy job."""
    pages = payload if isinstance(payload, list) else [payload]
    runs: list[Mapping[str, object]] = []
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        for run in page.get("workflow_runs", []):
            if isinstance(run, Mapping) and run.get("head_branch", "main") == "main":
                if str(run.get("conclusion") or "").lower() not in {"cancelled", "skipped", "neutral"}:
                    runs.append(run)
    runs.sort(key=lambda run: (str(run.get("updated_at") or ""), int(run.get("id", 0))), reverse=True)
    for run in runs:
        jobs = run.get("jobs")
        if jobs is None and callable(jobs_loader):
            jobs = jobs_loader(int(run["id"]))
            if jobs is not None:
                run["jobs"] = jobs
        if jobs is None:
            # Without job evidence we cannot prove this run attempted deploy.
            # Preserve existing incidents and retry on the next scheduled pass.
            return None
        if any(job.get("name") == DEPLOY_JOB_NAME for job in mappings(jobs)):
            return run
    return None

MARKER = "<!-- comic-pile-deploy-recovery:v1 -->"
TITLE = "Production deployment failed: repair release"
SMOKES = frozenset(
    {"Smoke production Rust health", "Smoke production rejected login on the Rust runtime"}
)


def gh_json(arguments: list[str]) -> object:
    """Read GitHub JSON, raising on API errors rather than inventing state."""
    try:
        result = subprocess.run(
            ["gh", *arguments], check=True, capture_output=True, text=True, timeout=120
        )
    except subprocess.CalledProcessError as exc:
        message = f"{exc.stderr or ''}\n{exc.stdout or ''}".lower()
        if any(marker in message for marker in (
            "rate limit exceeded", "secondary rate limit", "abuse detection"
        )):
            raise GitHubQuotaDeferredError("GitHub API quota exhausted") from None
        raise
    return json.loads(result.stdout)


def mappings(value: object) -> list[Mapping[str, object]]:
    """Validate an API list before acting on it."""
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("Expected a GitHub object list")
    return [item for item in value if isinstance(item, Mapping)]


def repair_candidates(
    failed: Mapping[str, object], pulls: Sequence[Mapping[str, object]]
) -> list[tuple[int, str]]:
    """Select repairs merged after the failed attempt, never reusing an old fix.

    The caller must additionally prove ancestry in the checked-out main. Merged
    deploy-fix PRs retain the repository's ordinary exact-head CI/review gates.
    Labels on unmerged PRs and arbitrary workflow input never grant a retry.
    """
    completed = str(failed.get("updated_at") or "")
    if not completed:
        return []
    failed_at = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    candidates: list[tuple[int, str]] = []
    for pull in pulls:
        merged = str(pull.get("mergedAt") or "")
        commit = pull.get("mergeCommit")
        number = pull.get("number")
        if pull.get("baseRefName") != "main" or not merged or not isinstance(commit, Mapping):
            continue
        labels = mappings(pull.get("labels"))
        if not any(label.get("name") == "deploy-fix" for label in labels):
            continue
        sha = str(commit.get("oid") or "")
        if (
            isinstance(number, int)
            and len(sha) == 40
            and all(char in "0123456789abcdef" for char in sha)
            and datetime.fromisoformat(merged.replace("Z", "+00:00")) > failed_at
        ):
            candidates.append((number, sha))
    return candidates


def merged_repair(failed: Mapping[str, object], repository: str) -> int | None:
    """Prove a new merged deploy-fix is contained in current main."""
    pulls = mappings(
        gh_json(
            [
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "merged",
                "--base",
                "main",
                "--label",
                "deploy-fix",
                "--limit",
                "100",
                "--json",
                "number,mergeCommit,mergedAt,baseRefName,labels",
            ]
        )
    )
    for number, sha in repair_candidates(failed, pulls):
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "HEAD"], check=False, timeout=30
        )
        if result.returncode == 0:
            return number
    return None


def successful_smokes(run: Mapping[str, object]) -> bool:
    """Require actual successful migration/deploy and both post-deploy probes."""
    for job in mappings(run.get("jobs") or []):
        if job.get("name") != DEPLOY_JOB_NAME or job.get("conclusion") != "success":
            continue
        passed = {
            str(step.get("name"))
            for step in mappings(job.get("steps"))
            if step.get("conclusion") == "success"
        }
        return SMOKES <= passed
    return False


def attempt_revision(run: Mapping[str, object], repository: str) -> str | None:
    """Read selected SHA evidence for this exact run attempt, never its trigger SHA.

    Historical/expired artifacts and failed pre-checkout runs have no trustworthy
    selected revision. Missing evidence is reported explicitly, without guessing.
    """
    run_id = run.get("id")
    attempt = run.get("run_attempt")
    if not isinstance(run_id, int) or not isinstance(attempt, int):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="deployment-attempt-") as directory:
            subprocess.run(
                [
                    "gh", "run", "download", str(run_id), "--repo", repository,
                    "--name", f"deployment-attempt-{attempt}", "--dir", directory,
                ],
                check=True, capture_output=True, text=True, timeout=60,
            )
            path = Path(directory) / "deployment-attempt.json"
            if path.stat().st_size > 4096:
                return None
            payload = json.loads(path.read_text())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    sha = payload.get("sha")
    if (
        payload.get("run_id") == str(run_id)
        and payload.get("run_attempt") == str(attempt)
        and isinstance(sha, str)
        and len(sha) == 40
        and all(char in "0123456789abcdef" for char in sha)
    ):
        return sha
    return None


def incident_body(run: Mapping[str, object], selected_sha: str | None = None) -> str:
    """Build sanitized repair instructions from run metadata, never raw logs."""
    failures: list[str] = []
    for job in mappings(run.get("jobs") or []):
        for step in mappings(job.get("steps", [])):
            if step.get("conclusion") in RED_CONCLUSIONS:
                failures.append(str(step.get("name")))
    revision = (
        f"`{selected_sha}`" if selected_sha else
        "unavailable (no valid exact-attempt revision artifact; do not infer from trigger SHA)"
    )
    return (
        f"{MARKER}\n\nOwner objective: restore verified production delivery automatically through "
        "a reviewed repair.\n\n"
        f"Failed attempt: {run.get('html_url')}\n"
        f"Selected deployment attempt SHA: {revision}.\n"
        f"Workflow trigger SHA: `{run.get('head_sha')}`.\n"
        f"Failed steps: {', '.join(failures) or 'inspect linked job'}.\n\n"
        "Investigate the linked run and actual live deployment. Fix the cause; do not weaken "
        "smoke assertions or bypass schema, CI, independent review, or production gates. "
        "Open a ready-for-review PR labeled `deploy-fix` referencing this issue. "
        "A new merged deploy-fix on main permits one recovery attempt through the full "
        "deployment pipeline. A repeat failure remains frozen and needs a newer repair.\n\n"
        "Acceptance: successful actual deployment, exact production revision verified, "
        "both health and rejected-login probes passing. Keep this incident open until "
        "that proof exists. Preserve current worker ownership while updating evidence.\n"
    )


def reconcile(repository: str) -> None:
    """Create/update one repair incident or close it after proven recovery."""
    # Bounded current evidence, not the entire historical deployment ledger.
    # The former --paginate --slurp loop exhausted installation REST quota.
    payload = gh_json(
        ["api", f"repos/{repository}/actions/workflows/deploy-production.yml/runs?per_page=20"]
    )
    latest = latest_attempted_deploy_run(
        payload, jobs_loader=lambda run_id: fetch_run_jobs(run_id, repository=repository)
    )
    if latest is None:
        return
    issues = mappings(
        gh_json(
            [
                "issue",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--label",
                "deploy-fix",
                "--limit",
                "100",
                "--json",
                "number,body,labels",
            ]
        )
    )
    incidents = [issue for issue in issues if MARKER in str(issue.get("body") or "")]
    if latest.get("conclusion") in RED_CONCLUSIONS:
        body = incident_body(latest, attempt_revision(latest, repository))
        if incidents:
            # Body-only updates keep active leases and review state untouched.
            for issue in incidents:
                if issue.get("body") != body:
                    gh_json(
                        [
                            "api",
                            "--method",
                            "PATCH",
                            f"repos/{repository}/issues/{issue['number']}",
                            "-f",
                            f"body={body}",
                        ]
                    )
        else:
            gh_json(
                [
                    "api",
                    "--method",
                    "POST",
                    f"repos/{repository}/issues",
                    "-f",
                    f"title={TITLE}",
                    "-f",
                    f"body={body}",
                    *[
                        argument
                        for label in (
                            "bug",
                            "infrastructure",
                            "deploy-fix",
                            "priority:P0",
                            "ralph-task",
                            "ralph-priority:critical",
                            "ralph-status:pending",
                            "factory",
                            "factory:unowned",
                        )
                        for argument in ("-f", f"labels[]={label}")
                    ],
                ]
            )
    elif successful_smokes(latest):
        for issue in incidents:
            labels = {str(label.get("name")) for label in mappings(issue.get("labels", []))}
            # Let a live worker finish its own issue; never erase its lease.
            if any(
                label.startswith("factory:")
                and label
                not in {
                    "factory:unowned",
                    "factory:building",
                    "factory:review",
                    "factory:ci",
                    "factory:ready",
                    "factory:blocked",
                    "factory:changes-requested",
                }
                for label in labels
            ):
                continue
            done_labels = sorted(
                {label for label in labels if not label.startswith(("factory:", "ralph-status:"))}
                | {"ralph-status:done"}
            )
            gh_json(
                [
                    "api",
                    "--method",
                    "POST",
                    f"repos/{repository}/issues/{issue['number']}/comments",
                    "-f",
                    f"body=Recovery verified by {latest.get('html_url')}: actual deployment "
                    "and both production smoke checks passed.",
                ]
            )
            gh_json(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{repository}/issues/{issue['number']}",
                    "-f",
                    "state=closed",
                    "-f",
                    "state_reason=completed",
                    *[
                        argument
                        for label in done_labels
                        for argument in ("-f", f"labels[]={label}")
                    ],
                ]
            )


def main() -> None:
    """Reconcile using the repository selected by the trusted workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        reconcile(os.environ["GITHUB_REPOSITORY"])
    except GitHubQuotaDeferredError:
        # No issue mutation or deployment-success assertion without evidence.
        # The 15-minute scheduled recovery will retry after quota reset.
        print("Production recovery deferred: GitHub API rate limited; existing incidents unchanged")


if __name__ == "__main__":
    main()
